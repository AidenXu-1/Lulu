"""路 A:匿名抓抖音作者主页「最新一页 ~20 条」。

守 ADR-0004/0013:自有 HTTP 客户端,匿名、不读浏览器 cookie、不登录。
配方(已 spike 实证):公开 ttwid 注册接口换真 ttwid + a_bogus 签名 + 随机 msToken。
深翻页(全量)被抖音登录墙挡住,属路 B(登录态浏览器),不在本模块。
"""

from __future__ import annotations

import json
import hashlib
import random
import re
import socket
import ssl
import string
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from http import cookiejar
from http.client import IncompleteRead
from dataclasses import dataclass

from ._abogus import ABogus
from .models import CollectedItem, dedupe, from_aweme

# 默认 TLS 上下文:校验主机名 + 校验证书链(防中间人篡改/窃听)。
# 历史上 spike 阶段曾关掉校验图省事,已移除——本地默认必须走可信连接。
_SSL = ssl.create_default_context()

_DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# Same Mac/Chrome identity and screen as the session headers and API parameters.
# Do not let ABogus's Windows/Edge defaults silently replace that identity.
_DESKTOP_FP = "1920|969|1920|1080|0|0|0|0|1920|1080|1920|1055|1920|969|24|24|MacIntel"
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
_SEC_UID_RE = re.compile(r"MS4w[\w-]+")
_WORK_ID_RE = re.compile(r"^[0-9]{1,64}$")
# 从整段分享文案里抠出第一个链接(分享口令前后都是中文/空格)
_URL_RE = re.compile(r"https?://[^\s'\"，。！？、）)】<>]+")
_POST_API = "https://www.douyin.com/aweme/v1/web/aweme/post/"
_DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
_ANONYMOUS_REQUEST_TIMEOUT_S = 20
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

_NEED_PROFILE_MSG = (
    "没识别到作者主页。\n"
    "请用「作者主页」的完整链接(douyin.com/user/... ):\n"
    "  · 抖音 App 进 TA 主页 → 右上角分享 → 复制链接;或\n"
    "  · 电脑浏览器打开 TA 主页,复制地址栏链接。\n"
    "(也可以粘贴 TA 任意一条「视频」的分享链接。)"
)


class DouyinCollectError(RuntimeError):
    """对用户可见的采集错误(链接无效 / 网络 / 平台改版)。"""

    def __init__(self, message, *, code="DOUYIN_FETCH_FAILED", items=()):
        super().__init__(message)
        self.code = code
        self.items = tuple(items)


def _http_failure(status: int) -> DouyinCollectError:
    causes = {
        401: ("抖音要求登录后访问。", "DOUYIN_LOGIN_REQUIRED"),
        403: ("抖音拒绝了本次访问。", "DOUYIN_ACCESS_RESTRICTED"),
        429: ("抖音暂时限制了访问频率，请稍后重试。", "DOUYIN_RATE_LIMITED"),
    }
    message, code = causes.get(status, (
        "抖音服务暂时无法返回作品，请稍后重试。", "DOUYIN_SERVICE_ERROR"))
    return DouyinCollectError(message, code=code)


def _rand_mstoken(n: int = 126) -> str:
    al = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(al) for _ in range(n))


def _check_cancelled(is_cancelled=None):
    if is_cancelled and is_cancelled():
        raise InterruptedError("采集已取消")


def _read_anonymous_response(response, limit, *, is_cancelled=None, deadline):
    """Bound body reads and wake a blocked urllib socket on cancel or deadline.

    Closing a buffered response in another thread can itself wait for read's
    lock. Shut down only this response's socket, then let its owner close it.
    The watcher is joined before returning; no request thread is abandoned.
    """
    _check_cancelled(is_cancelled)
    done, expired = threading.Event(), threading.Event()
    connection = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)

    def watch():
        while not done.wait(.05):
            cancelled = is_cancelled is not None and is_cancelled()
            timed_out = time.monotonic() >= deadline
            if cancelled or timed_out:
                if timed_out:
                    expired.set()
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # The reader or peer may already have closed it.
                return

    watcher = None
    if isinstance(connection, socket.socket):
        watcher = threading.Thread(target=watch, name="lulu-douyin-response", daemon=True)
        watcher.start()
    try:
        try:
            body = response.read(limit)
        except Exception:
            _check_cancelled(is_cancelled)
            if expired.is_set():
                raise TimeoutError("抖音响应读取超时。") from None
            raise
        _check_cancelled(is_cancelled)
        if expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("抖音响应读取超时。")
        return body
    finally:
        done.set()
        if watcher is not None:
            watcher.join()


def _trusted_douyin_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        return (parts.scheme in {"https", "http"} and parts.port in (None, 80, 443)
                and parts.username is None and parts.password is None
                and any(host == root or host.endswith("." + root)
                        for root in ("douyin.com", "iesdouyin.com")))
    except (ValueError, AttributeError):
        return False


def _resolve_douyin_link(profile_url: str, *, is_cancelled=None):
    """Expand a short link to an exact homepage/work without reading its author."""
    from .router import detect_link
    _check_cancelled(is_cancelled)
    identity = detect_link(profile_url)
    if identity.platform != "douyin":
        raise DouyinCollectError(_NEED_PROFILE_MSG, code="DOUYIN_INVALID_PROFILE")
    url = identity.canonical_url
    if identity.kind == "unknown" and urllib.parse.urlsplit(url).hostname != "v.douyin.com":
        raise DouyinCollectError(_NEED_PROFILE_MSG, code="DOUYIN_INVALID_PROFILE")
    if identity.kind == "unknown":
        class _Track(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                _check_cancelled(is_cancelled)
                if not _trusted_douyin_url(newurl):
                    raise DouyinCollectError(_NEED_PROFILE_MSG, code="DOUYIN_INVALID_PROFILE")
                return super().redirect_request(req, fp, code, msg, headers, newurl)
        op = urllib.request.build_opener(_Track, urllib.request.HTTPSHandler(context=_SSL))
        try:
            with op.open(urllib.request.Request(url, headers={"User-Agent": _MOBILE_UA}), timeout=15) as resp:
                final = resp.geturl()
            _check_cancelled(is_cancelled)
        except urllib.error.HTTPError as exc:
            raise _http_failure(exc.code) from exc
        except InterruptedError:
            raise
        except (urllib.error.URLError, OSError) as exc:
            raise DouyinCollectError("暂时无法连接抖音，请检查网络后重试。", code="DOUYIN_NETWORK_ERROR") from exc
        if not _trusted_douyin_url(final):
            raise DouyinCollectError(_NEED_PROFILE_MSG, code="DOUYIN_INVALID_PROFILE")
        parts = urllib.parse.urlsplit(final)
        # Mobile work redirects use the same stable numeric work ID.
        mobile = re.fullmatch(r"/share/video/([0-9]{1,64})/?", parts.path)
        mobile_author = re.fullmatch(r"/share/user/(MS4w[\w-]+)/?", parts.path)
        if mobile_author:
            final = "https://www.douyin.com/user/" + mobile_author[1]
        identity = detect_link("https://www.douyin.com/video/" + mobile[1] if mobile else final)
    if identity.kind == "homepage" and _SEC_UID_RE.fullmatch(identity.extracted_id):
        return identity
    if identity.kind == "work" and _WORK_ID_RE.fullmatch(identity.extracted_id):
        return identity
    raise DouyinCollectError(_NEED_PROFILE_MSG, code="DOUYIN_INVALID_PROFILE")


def resolve_sec_uid(profile_url: str, *, is_cancelled=None) -> str:
    """Bind the exact homepage path or anonymous work detail to its author."""
    identity = _resolve_douyin_link(profile_url, is_cancelled=is_cancelled)
    if identity.kind == "homepage":
        return identity.extracted_id
    if identity.kind == "work":
        options = {"is_cancelled": is_cancelled} if is_cancelled is not None else {}
        item = fetch_item(identity.extracted_id, **options)
        _check_cancelled(is_cancelled)
        if item.work_id == identity.extracted_id and _SEC_UID_RE.fullmatch(item.author_id):
            return item.author_id
    raise DouyinCollectError(_NEED_PROFILE_MSG, code="DOUYIN_INVALID_PROFILE")


@dataclass(frozen=True)
class _AnonymousSession:
    cookie: str
    uifid: str


def _register_ttwid(*, is_cancelled=None) -> _AnonymousSession:
    """匿名调字节公开 ttwid 注册接口换真 ttwid(无登录、无账号)。"""
    _check_cancelled(is_cancelled)
    payload = json.dumps({
        "region": "cn", "aid": 6383, "needFid": False, "service": "www.douyin.com",
        "migrate_info": {"ticket": "", "source": "node"}, "cbUrlProtocol": "https", "union": True,
    }).encode()
    cj = cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                     urllib.request.HTTPSHandler(context=_SSL))
    try:
        # 公开网页与详情 API 属于同一个匿名会话。仅 ttwid 的旧请求会被
        # Argus 拒绝；保留服务器给此会话的第一方 Cookie，不借用浏览器登录态。
        deadline = time.monotonic() + 15
        with op.open(urllib.request.Request(
            "https://www.douyin.com/",
            headers={"User-Agent": _DESKTOP_UA}), timeout=15) as resp:
            visitor = resp.headers.get('x-tt-trace-host', '')
            _read_anonymous_response(resp, 300_000, is_cancelled=is_cancelled, deadline=deadline)
        _check_cancelled(is_cancelled)
        deadline = time.monotonic() + 15
        with op.open(urllib.request.Request(
            "https://ttwid.bytedance.com/ttwid/union/register/", data=payload,
            headers={"Content-Type": "application/json", "User-Agent": _DESKTOP_UA}), timeout=15) as resp:
            _read_anonymous_response(resp, 64_000, is_cancelled=is_cancelled, deadline=deadline)
        _check_cancelled(is_cancelled)
    except urllib.error.HTTPError as exc:
        raise _http_failure(exc.code) from exc
    except InterruptedError:
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise DouyinCollectError("暂时无法连接抖音，请检查网络后重试。", code="DOUYIN_NETWORK_ERROR") from exc
    cookie = "; ".join(f"{c.name}={c.value}" for c in cj)
    if "ttwid=" not in cookie:
        raise DouyinCollectError("抖音未能建立匿名访问会话，请稍后重试。", code="DOUYIN_EMPTY_RESPONSE")
    # The visitor identity belongs to this memory-only anonymous session, not
    # the user's browser. A current API request also signs its exact query.
    visitor = next((c.value for c in cj if c.name.lower().replace('_', '')
                    in {'uifid', 'uifidtemp'}), visitor)
    return _AnonymousSession(cookie, visitor if isinstance(visitor, str) else '')


def _signed_anonymous_params(params: str, session: _AnonymousSession) -> tuple[str, str]:
    if not session.uifid or len(session.uifid) > 2048 or any(ord(c) < 33 for c in session.uifid):
        raise DouyinCollectError('抖音未能建立匿名访客会话，请稍后重试。', code='DOUYIN_EMPTY_RESPONSE')
    query = dict(urllib.parse.parse_qsl(params, keep_blank_values=True))
    query.update(uifid=session.uifid, timestamp=str(int(time.time())))
    _, bogus, ua, _ = ABogus(user_agent=_DESKTOP_UA, fp=_DESKTOP_FP).generate_abogus(urllib.parse.urlencode(query))
    query['a_bogus'] = bogus
    encoded = urllib.parse.urlencode(query)
    # Public web protocol; the salt is not a credential. See the verified
    # upstream analysis linked in docs/verification/homepage-session-repair-20261108.md.
    value = f'{session.uifid}_{query["timestamp"]}_A96D855A08C0A9707F8BEF0D9A527E4E_{encoded}'
    query['x-secsdk-web-signature'] = hashlib.md5(value.encode()).hexdigest()
    return urllib.parse.urlencode(query), ua


def _build_params(sec_uid: str, count: int) -> str:
    return urllib.parse.urlencode({
        "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
        "sec_user_id": sec_uid, "max_cursor": "0", "locate_query": "false",
        "show_live_replay_strategy": "1", "need_time_list": "1", "time_list_query": "0",
        "whale_cut_token": "", "cut_version": "1", "count": str(count),
        "publish_video_strategy_type": "2", "from_user_page": "1", "update_version_code": "170400",
        "pc_client_type": "1", "version_code": "170400", "version_name": "17.4.0",
        "cookie_enabled": "true", "screen_width": "1920", "screen_height": "1080",
        "browser_language": "zh-CN", "browser_platform": "MacIntel", "browser_name": "Chrome",
        "browser_version": "126.0.0.0", "browser_online": "true", "engine_name": "Blink",
        "engine_version": "126.0.0.0", "os_name": "Mac OS", "os_version": "10.15.7",
        "cpu_core_num": "8", "device_memory": "8", "platform": "PC", "downlink": "10",
        "effective_type": "4g", "round_trip_time": "50", "msToken": _rand_mstoken(),
    })


def parse_aweme_list(payload: dict) -> list[CollectedItem]:
    """把接口返回 JSON 解析成去重后的 CollectedItem 列表(纯函数,可独立测试)。"""
    items = [from_aweme(a) for a in (payload.get("aweme_list") or [])]
    return dedupe(items)


def parse_author_page(payload: object, sec_uid: str) -> list[CollectedItem]:
    """Accept a successful first page only when every work proves its author."""
    changed = "抖音返回的作品数据不完整或格式已变化，请更新 Lulu 后重试。"
    if not isinstance(payload, dict) or type(payload.get("status_code")) is not int:
        raise DouyinCollectError(changed, code="DOUYIN_PLATFORM_CHANGED")
    if payload["status_code"] != 0:
        raise DouyinCollectError("抖音服务未返回成功的作品结果，请稍后重试。", code="DOUYIN_SERVICE_ERROR")
    works = payload.get("aweme_list")
    if not isinstance(works, list):
        raise DouyinCollectError(changed, code="DOUYIN_PLATFORM_CHANGED")
    if not works:
        raise DouyinCollectError("没有读取到作品，目前无法确认主页真实为空。", code="DOUYIN_EMPTY_LIST")
    for work in works:
        if not isinstance(work, dict):
            raise DouyinCollectError(changed, code="DOUYIN_PLATFORM_CHANGED")
        author = work.get("author")
        if (not _WORK_ID_RE.fullmatch(str(work.get("aweme_id") or ""))
                or not isinstance(author, dict) or author.get("sec_uid") != sec_uid):
            raise DouyinCollectError(changed, code="DOUYIN_PLATFORM_CHANGED")
    try:
        return parse_aweme_list(payload)
    except (TypeError, ValueError, AttributeError) as exc:
        raise DouyinCollectError(changed, code="DOUYIN_PLATFORM_CHANGED") from exc


def parse_aweme_detail(payload: dict, expected_work_id: str) -> CollectedItem:
    """只接受状态成功且身份与请求完全一致的一条作品详情。"""

    work_id = str(expected_work_id or "")
    if _WORK_ID_RE.fullmatch(work_id) is None:
        raise DouyinCollectError("作品身份无效，无法刷新音视频地址。", code="DOUYIN_INVALID_PROFILE")
    if not isinstance(payload, dict) or type(payload.get("status_code")) is not int:
        raise DouyinCollectError("作品详情格式无法识别，请重新采集主页。", code="DOUYIN_PLATFORM_CHANGED")
    if payload["status_code"] != 0:
        raise DouyinCollectError("抖音暂时没有返回成功的作品详情，请稍后重试。", code="DOUYIN_SERVICE_ERROR")
    detail = payload.get("aweme_detail")
    if not isinstance(detail, dict):
        raise DouyinCollectError("抖音没有返回可用的作品详情，请稍后重试。", code="DOUYIN_EMPTY_DETAIL")
    try:
        item = from_aweme(detail)
    except (TypeError, ValueError, AttributeError) as exc:
        raise DouyinCollectError("作品详情格式无法识别，请重新采集主页。", code="DOUYIN_PLATFORM_CHANGED") from exc
    if item.work_id != work_id:
        raise DouyinCollectError("作品详情身份不一致，已停止下载。", code="DOUYIN_PLATFORM_CHANGED")
    return item


def _build_detail_params(work_id: str) -> str:
    return urllib.parse.urlencode({
        "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
        "aweme_id": work_id, "update_version_code": "170400", "pc_client_type": "1",
        "version_code": "170400", "version_name": "17.4.0", "cookie_enabled": "true",
        "screen_width": "1920", "screen_height": "1080", "browser_language": "zh-CN",
        "browser_platform": "MacIntel", "browser_name": "Chrome",
        "browser_version": "126.0.0.0", "browser_online": "true",
        "engine_name": "Blink", "engine_version": "126.0.0.0", "os_name": "Mac OS",
        "os_version": "10.15.7", "cpu_core_num": "8", "device_memory": "8",
        "platform": "PC", "downlink": "10", "effective_type": "4g",
        "round_trip_time": "50", "msToken": _rand_mstoken(),
    })


def fetch_item(work_id: str, *, is_cancelled=None) -> CollectedItem:
    """匿名按稳定作品 ID 刷新一条抖音详情与临时播放地址。"""

    _check_cancelled(is_cancelled)
    identity = str(work_id or "")
    if _WORK_ID_RE.fullmatch(identity) is None:
        raise DouyinCollectError("作品身份无效，无法刷新音视频地址。", code="DOUYIN_INVALID_PROFILE")
    options = {"is_cancelled": is_cancelled} if is_cancelled is not None else {}
    session = _register_ttwid(**options)
    _check_cancelled(is_cancelled)
    params = _build_detail_params(identity)
    params, ua = _signed_anonymous_params(params, session)
    url = f"{_DETAIL_API}?{params}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ua,
            "Referer": f"https://www.douyin.com/video/{identity}",
            "Accept": "application/json, text/plain, */*",
            "Cookie": session.cookie,
        })
        deadline = time.monotonic() + _ANONYMOUS_REQUEST_TIMEOUT_S
        with urllib.request.urlopen(req, timeout=_ANONYMOUS_REQUEST_TIMEOUT_S, context=_SSL) as resp:
            body = _read_anonymous_response(resp, _MAX_RESPONSE_BYTES + 1,
                                            is_cancelled=is_cancelled, deadline=deadline)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise DouyinCollectError(f"抖音暂时拒绝匿名详情请求（HTTP {exc.code}）。", code="DOUYIN_ACCESS_RESTRICTED") from exc
        raise _http_failure(exc.code) from exc
    except InterruptedError:
        raise
    except (urllib.error.URLError, OSError, IncompleteRead) as exc:
        raise DouyinCollectError("网络不稳定，无法刷新音视频地址，请稍后重试。", code="DOUYIN_NETWORK_ERROR") from exc
    if not body:
        raise DouyinCollectError("抖音没有返回作品详情，请稍后重试。", code="DOUYIN_EMPTY_DETAIL")
    if len(body) > _MAX_RESPONSE_BYTES:
        raise DouyinCollectError("抖音作品详情超出当前可读取范围。", code="DOUYIN_PLATFORM_CHANGED")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DouyinCollectError("抖音作品详情格式已变化，请重新采集主页。", code="DOUYIN_EMPTY_DETAIL") from exc
    return parse_aweme_detail(payload, identity)


def fetch_item_with_page_fallback(work_id, progress, *, is_cancelled=None):
    try:
        if is_cancelled is None:
            return fetch_item(work_id)
        return fetch_item(work_id, is_cancelled=is_cancelled)
    except DouyinCollectError as exc:
        if exc.code not in {"DOUYIN_ACCESS_RESTRICTED", "DOUYIN_EMPTY_DETAIL"}:
            raise
        _check_cancelled(is_cancelled)
        from .douyin_public_page import fetch_rendered_item
        return fetch_rendered_item(work_id, progress)


def fetch_latest(profile_url: str, count: int = 20, *, is_cancelled=None) -> list[CollectedItem]:
    """路 A:抓该作者最新一页作品(约 15~20 条,抖音返回数不定)+ 9 项数据。匿名、不登录。"""
    _check_cancelled(is_cancelled)
    sec_uid = resolve_sec_uid(profile_url, is_cancelled=is_cancelled)
    try:
        return _fetch_author_latest(sec_uid, count, is_cancelled=is_cancelled)
    except DouyinCollectError as exc:
        exc.resolved_author_id = sec_uid
        raise


def _fetch_author_latest(sec_uid, count, *, is_cancelled=None):
    session = _register_ttwid(is_cancelled=is_cancelled)
    _check_cancelled(is_cancelled)
    params = _build_params(sec_uid, count)
    params, ua = _signed_anonymous_params(params, session)
    url = f"{_POST_API}?{params}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ua, "Referer": f"https://www.douyin.com/user/{sec_uid}",
            "Accept": "application/json, text/plain, */*", "Cookie": session.cookie})
        deadline = time.monotonic() + _ANONYMOUS_REQUEST_TIMEOUT_S
        with urllib.request.urlopen(req, timeout=_ANONYMOUS_REQUEST_TIMEOUT_S, context=_SSL) as resp:
            raw_body = _read_anonymous_response(resp, _MAX_RESPONSE_BYTES + 1,
                                                is_cancelled=is_cancelled, deadline=deadline)
        _check_cancelled(is_cancelled)
    except urllib.error.HTTPError as exc:
        raise _http_failure(exc.code) from exc
    except InterruptedError:
        raise
    except (urllib.error.URLError, OSError, IncompleteRead) as exc:
        raise DouyinCollectError("暂时无法连接抖音，请检查网络后重试。", code="DOUYIN_NETWORK_ERROR") from exc
    if len(raw_body) > _MAX_RESPONSE_BYTES:
        raise DouyinCollectError("抖音返回的数据超出当前可读取范围。", code="DOUYIN_PLATFORM_CHANGED")
    if not raw_body.strip():
        raise DouyinCollectError("抖音没有返回可读取的作品数据。", code="DOUYIN_EMPTY_RESPONSE")
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DouyinCollectError("抖音返回的数据格式已变化，请更新 Lulu 后重试。", code="DOUYIN_PLATFORM_CHANGED") from exc
    return parse_author_page(payload, sec_uid)
