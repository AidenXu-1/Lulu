"""路 A:匿名抓抖音作者主页「最新一页 ~20 条」。

守 ADR-0004/0013:自有 HTTP 客户端,匿名、不读浏览器 cookie、不登录。
配方(已 spike 实证):公开 ttwid 注册接口换真 ttwid + a_bogus 签名 + 随机 msToken。
深翻页(全量)被抖音登录墙挡住,属路 B(登录态浏览器),不在本模块。
"""

from __future__ import annotations

import json
import random
import re
import ssl
import string
import urllib.parse
import urllib.request
from http import cookiejar

from ._abogus import ABogus
from .models import CollectedItem, dedupe, from_aweme

# 默认 TLS 上下文:校验主机名 + 校验证书链(防中间人篡改/窃听)。
# 历史上 spike 阶段曾关掉校验图省事,已移除——本地默认必须走可信连接。
_SSL = ssl.create_default_context()

_DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
_SEC_UID_RE = re.compile(r"MS4w[\w-]+")
_WORK_ID_RE = re.compile(r"^[0-9]{1,64}$")
# 从整段分享文案里抠出第一个链接(分享口令前后都是中文/空格)
_URL_RE = re.compile(r"https?://[^\s'\"，。！？、）)】<>]+")
_POST_API = "https://www.douyin.com/aweme/v1/web/aweme/post/"
_DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

_NEED_PROFILE_MSG = (
    "没识别到作者主页。\n"
    "请用「作者主页」的完整链接(douyin.com/user/... ):\n"
    "  · 抖音 App 进 TA 主页 → 右上角分享 → 复制链接;或\n"
    "  · 电脑浏览器打开 TA 主页,复制地址栏链接。\n"
    "(也可以粘贴 TA 任意一条「视频」的分享链接。)"
)


class DouyinCollectError(RuntimeError):
    """对用户可见的采集错误(链接无效 / 网络 / 平台改版)。"""

    def __init__(self, message, *, code="DOUYIN_FETCH_FAILED"):
        super().__init__(message)
        self.code = code


def _rand_mstoken(n: int = 126) -> str:
    al = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(al) for _ in range(n))


def resolve_sec_uid(profile_url: str) -> str:
    """从输入解析作者 sec_uid。

    支持:① 含 MS4w… 的主页链接 ② 整段分享文案(自动抠 URL)③ 短链/视频链接
    (跟踪重定向 + 扫页面里嵌的作者 sec_uid)。识别不到给可操作的指引。
    """
    raw = (profile_url or "").strip()
    # 直接含 sec_uid(douyin.com/user/MS4w...)
    m = _SEC_UID_RE.search(raw)
    if m:
        return m.group(0)
    # 从整段文案里抠出第一个 URL
    um = _URL_RE.search(raw)
    if not um:
        raise DouyinCollectError("没识别到链接。请粘贴作者主页链接(douyin.com/user/... )。")
    url = um.group(0)
    m = _SEC_UID_RE.search(url)
    if m:
        return m.group(0)

    # 短链/视频链接:跟踪重定向(含中间 Location),扫 sec_uid
    mid: dict = {}

    class _Track(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            mm = _SEC_UID_RE.search(newurl)
            if mm:
                mid.setdefault("sec", mm.group(0))
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    op = urllib.request.build_opener(_Track, urllib.request.HTTPSHandler(context=_SSL))
    try:
        with op.open(urllib.request.Request(url, headers={"User-Agent": _MOBILE_UA}), timeout=15) as resp:
            final = resp.geturl()
            body = resp.read(300_000).decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001
        raise DouyinCollectError("打不开这个链接 —— 可能网络不稳定,请稍后重试。") from exc
    m = _SEC_UID_RE.search(final) or _SEC_UID_RE.search(body)
    if m:
        return m.group(0)
    if "sec" in mid:
        return mid["sec"]
    raise DouyinCollectError(_NEED_PROFILE_MSG)


def _register_ttwid() -> str:
    """匿名调字节公开 ttwid 注册接口换真 ttwid(无登录、无账号)。"""
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
        with op.open(urllib.request.Request(
            "https://www.douyin.com/",
            headers={"User-Agent": _DESKTOP_UA}), timeout=15) as resp:
            resp.read(300_000)
        with op.open(urllib.request.Request(
            "https://ttwid.bytedance.com/ttwid/union/register/", data=payload,
            headers={"Content-Type": "application/json", "User-Agent": _DESKTOP_UA}), timeout=15) as resp:
            resp.read(64_000)
    except Exception as exc:  # noqa: BLE001
        raise DouyinCollectError("网络不稳定,初始化采集会话失败,请稍后重试。") from exc
    cookie = "; ".join(f"{c.name}={c.value}" for c in cj)
    if "ttwid=" not in cookie:
        raise DouyinCollectError("初始化采集会话失败(没拿到访问凭证),请稍后重试。")
    return cookie


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


def parse_aweme_detail(payload: dict, expected_work_id: str) -> CollectedItem:
    """只接受状态成功且身份与请求完全一致的一条作品详情。"""

    work_id = str(expected_work_id or "")
    if _WORK_ID_RE.fullmatch(work_id) is None:
        raise DouyinCollectError("作品身份无效，无法刷新音视频地址。")
    if not isinstance(payload, dict) or payload.get("status_code") != 0:
        raise DouyinCollectError("抖音没有返回可用的作品详情，请稍后重试。")
    detail = payload.get("aweme_detail")
    if not isinstance(detail, dict):
        raise DouyinCollectError("抖音没有返回可用的作品详情，请稍后重试。", code="DOUYIN_EMPTY_DETAIL")
    item = from_aweme(detail)
    if item.work_id != work_id:
        raise DouyinCollectError("作品详情身份不一致，已停止下载。")
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


def fetch_item(work_id: str) -> CollectedItem:
    """匿名按稳定作品 ID 刷新一条抖音详情与临时播放地址。"""

    identity = str(work_id or "")
    if _WORK_ID_RE.fullmatch(identity) is None:
        raise DouyinCollectError("作品身份无效，无法刷新音视频地址。")
    cookie = _register_ttwid()
    params = _build_detail_params(identity)
    _, a_bogus, ua, _ = ABogus().generate_abogus(params)
    url = f"{_DETAIL_API}?{params}&a_bogus={urllib.parse.quote(a_bogus)}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ua,
            "Referer": f"https://www.douyin.com/video/{identity}",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie,
        })
        with urllib.request.urlopen(req, timeout=20, context=_SSL) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 429):
            raise DouyinCollectError(f"抖音暂时拒绝匿名详情请求（HTTP {exc.code}）。", code="DOUYIN_ACCESS_RESTRICTED") from exc
        raise DouyinCollectError(f"抖音详情请求失败（HTTP {exc.code}）。") from exc
    except Exception as exc:  # noqa: BLE001
        raise DouyinCollectError("网络不稳定，无法刷新音视频地址，请稍后重试。") from exc
    if not body:
        raise DouyinCollectError("抖音没有返回作品详情，请稍后重试。", code="DOUYIN_EMPTY_DETAIL")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DouyinCollectError("抖音作品详情格式已变化，请重新采集主页。", code="DOUYIN_EMPTY_DETAIL") from exc
    return parse_aweme_detail(payload, identity)


def fetch_item_with_page_fallback(work_id, progress):
    try:
        return fetch_item(work_id)
    except DouyinCollectError as exc:
        if exc.code not in {"DOUYIN_ACCESS_RESTRICTED", "DOUYIN_EMPTY_DETAIL"}:
            raise
        from .douyin_public_page import fetch_rendered_item
        return fetch_rendered_item(work_id, progress)


def fetch_latest(profile_url: str, count: int = 20) -> list[CollectedItem]:
    """路 A:抓该作者最新一页作品(约 15~20 条,抖音返回数不定)+ 9 项数据。匿名、不登录。"""
    sec_uid = resolve_sec_uid(profile_url)
    cookie = _register_ttwid()
    params = _build_params(sec_uid, count)
    _, a_bogus, ua, _ = ABogus().generate_abogus(params)
    url = f"{_POST_API}?{params}&a_bogus={urllib.parse.quote(a_bogus)}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ua, "Referer": f"https://www.douyin.com/user/{sec_uid}",
            "Accept": "application/json, text/plain, */*", "Cookie": cookie})
        with urllib.request.urlopen(req, timeout=20, context=_SSL) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001
        raise DouyinCollectError("网络不稳定,抓取失败,请稍后再试。") from exc
    if not body:
        raise DouyinCollectError(
            "抖音没有返回数据 —— 可能是临时风控,稍等片刻再试;若反复失败,可能是抖音改版需更新。")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DouyinCollectError("抖音返回了无法解析的数据,可能是平台改版,请反馈给开发者。") from exc
    return parse_aweme_list(payload)
