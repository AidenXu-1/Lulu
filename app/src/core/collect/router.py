"""三平台主页/作品链接的保守无网络识别。"""

from __future__ import annotations

import re
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .contracts import LinkIdentity

_URL_RE = re.compile(r"https?://[^\s'\"，。！？、）)】<>]+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ",.;:!?，。！？、；：）)]】》」』"

_DOUYIN_ID = r"[A-Za-z0-9._-]+"
_XHS_ID = r"[A-Za-z0-9_-]+"
_BILI_WORK_ID = r"(?:BV[A-Za-z0-9]+|av[0-9]+)"


def _unknown(reason_code: str = "invalid_input") -> LinkIdentity:
    return LinkIdentity(reason_code=reason_code)


def _host_is(host: str, root: str) -> bool:
    return host == root or host.endswith(f".{root}")


def _normalize_host(hostname: str) -> str:
    return hostname.rstrip(".").encode("idna").decode("ascii").lower()


def _canonical(parts: SplitResult, host: str) -> str:
    scheme = parts.scheme.lower()
    port = parts.port
    netloc = host if port is None or (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    ) else f"{host}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def _parse_candidate(url: str) -> LinkIdentity:
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"}:
            return _unknown()
        if parts.username is not None or parts.password is not None or not parts.hostname:
            return _unknown()
        port = parts.port
        if port is not None and port not in {80, 443}:
            return _unknown()
        host = _normalize_host(parts.hostname)
    except (UnicodeError, ValueError):
        return _unknown()

    canonical = _canonical(parts, host)
    path = parts.path or "/"

    if host == "v.douyin.com":
        return LinkIdentity("douyin", "unknown", canonical, "", "probable")
    if _host_is(host, "douyin.com"):
        homepage = re.fullmatch(rf"/user/({_DOUYIN_ID})/?", path, re.IGNORECASE)
        if homepage:
            return LinkIdentity("douyin", "homepage", canonical, homepage.group(1), "exact")
        work = re.fullmatch(rf"/video/({_DOUYIN_ID})/?", path, re.IGNORECASE)
        if work:
            return LinkIdentity("douyin", "work", canonical, work.group(1), "exact")
        return LinkIdentity("douyin", "unknown", canonical, "", "probable")

    if host == "xhslink.com" or host.endswith(".xhslink.com"):
        return LinkIdentity("xiaohongshu", "unknown", canonical, "", "probable")
    if _host_is(host, "xiaohongshu.com"):
        homepage = re.fullmatch(rf"/user/profile/({_XHS_ID})/?", path, re.IGNORECASE)
        if homepage:
            return LinkIdentity(
                "xiaohongshu", "homepage", canonical, homepage.group(1), "exact"
            )
        work = re.fullmatch(
            rf"/(?:explore|discovery/item)/({_XHS_ID})/?", path, re.IGNORECASE
        )
        if work:
            return LinkIdentity("xiaohongshu", "work", canonical, work.group(1), "exact")
        return LinkIdentity("xiaohongshu", "unknown", canonical, "", "probable")

    if host == "b23.tv":
        return LinkIdentity("bilibili", "unknown", canonical, "", "probable")
    if _host_is(host, "bilibili.com"):
        if host == "space.bilibili.com":
            homepage = re.fullmatch(r"/([0-9]+)/?", path)
            if homepage:
                return LinkIdentity(
                    "bilibili", "homepage", canonical, homepage.group(1), "exact"
                )
        work = re.fullmatch(rf"/video/({_BILI_WORK_ID})/?", path, re.IGNORECASE)
        if work:
            return LinkIdentity("bilibili", "work", canonical, work.group(1), "exact")
        return LinkIdentity("bilibili", "unknown", canonical, "", "probable")

    return _unknown("unsupported_link")


def detect_link(text: str) -> LinkIdentity:
    """从 URL 或分享文案识别一个链接;全程无网络。

    同一 URL 重复出现视为一个候选;两个不同受支持候选一律返回歧义。
    """
    raw = str(text or "").strip()
    candidates = [match.group(0).rstrip(_TRAILING_PUNCTUATION) for match in _URL_RE.finditer(raw)]
    if not candidates:
        return _unknown()

    recognized: dict[tuple[str, str, str], LinkIdentity] = {}
    unsupported: LinkIdentity | None = None
    for candidate in candidates:
        identity = _parse_candidate(candidate)
        if identity.platform == "unknown":
            unsupported = unsupported or identity
            continue
        recognized[(identity.platform, identity.kind, identity.canonical_url)] = identity

    if not recognized:
        return unsupported or _unknown()
    if len(recognized) > 1:
        return _unknown("ambiguous_link")
    return next(iter(recognized.values()))
