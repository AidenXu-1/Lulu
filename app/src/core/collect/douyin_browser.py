"""路 B:登录态全量采集 —— 驱动用户本机浏览器滚动作者主页,截获 aweme/post 取全量。

ADR-0013 opt-in 例外:用户显式选择 + 自行在浏览器登录(建议小号)+ 独立 profile 隔离
主浏览器 + App 不经手凭据 + 数据只留本地。匿名默认路在 douyin_fetch(本模块不碰)。

依赖 DrissionPage(驱动本机 Chrome,不内置 Chromium);仅本路使用,惰性导入。
"""

from __future__ import annotations

import random
import json
import os
import sys
import time
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .browser_shell import BrowserLaunchSpec, BrowserShell, BrowserShellError
from .models import CollectedItem
from .douyin_fetch import (
    resolve_sec_uid, DouyinCollectError, parse_author_page, parse_aweme_detail,
    _http_failure, _resolve_douyin_link, _SEC_UID_RE,
)
from .profiles import ProfileRegistry

ProgressCb = Callable[[str, int], None]
CancelCb = Callable[[], bool]

_POST_API = "aweme/v1/web/aweme/post"
_DETAIL_API = "aweme/v1/web/aweme/detail"
# 登录态标记(登录成功后写,UI 据此显示「已登录」);独立 profile 让登录持久复用
_LOGIN_MARK = ".logged_in"

# ---- 拟人化 / 降频 / 异常检测:内部常量,不暴露用户设置(见 spec douyin-path-b-humanized-collection)----
_SCROLL_STEPS = (2, 4)          # 每轮分几小步滚动
_SCROLL_PX = (500, 1000)        # 每小步滚动像素
_MICRO_WAIT = (0.3, 0.8)        # 小步之间微停(秒)
_ROUND_WAIT = (1.5, 3.5)        # 每轮主等待(秒)
_LONG_PAUSE = (4.0, 8.0)        # 偶发长停顿,模拟"看一下内容"(秒)
_LONG_PAUSE_EVERY = (5, 8)      # 每隔几轮一次长停顿
_IDLE_ROUNDS_TO_STOP = 3        # 只作无终点信号时的异常兜底，不再用来证明“采全”
_MAX_ROUNDS = 300               # 故障安全上限；正常路径由 has_more=0 结束
_MAX_PACKETS_PER_ROUND = 200    # 防止异常连续响应让单轮永不归还取消检查
_FIRST_PAGE_TIMEOUT_S = 45      # 首屏慢加载独立宽限，不按三次短轮询判空
_SESSION_CHECKS = 4             # page.get 后的短宽限，避免把 cookie 瞬时迟到当成失效
_SESSION_CHECK_WAIT_S = 1.0
_SESSION_COOKIES = {"sessionid", "sessionid_ss"}
_VERIFY_URL_HINTS = ("verify", "captcha", "secsdk", "verification")
_CAPTCHA_SELECTORS = (
    'css:.captcha_verify_container', 'css:.captcha-verify-container',
    'css:#captcha_container', 'css:.secsdk-captcha-drag-wrapper',
    'css:.vc_container', 'css:.captcha_verify_img--wrapper',
    'css:iframe[src*="verify"]',
)
_CAPTCHA_WAIT_S = 300           # 低风险模式:等用户手动过验证码的最长时间

# 内部采集模式；UI 只产生效率模式，低风险分支暂作一个发布周期的回滚兼容
MODE_EFFICIENCY = "efficiency"  # 默认:快速一次采完;撞验证码即停并保留已采集
MODE_LOW_RISK = "low_risk"      # 温和:撞验证码暂停,用户在浏览器手动过后自动续采,力求采全


def _human_wait() -> float:
    return random.uniform(*_ROUND_WAIT)


def _micro_wait() -> float:
    return random.uniform(*_MICRO_WAIT)


def _pick_scroll_amount() -> int:
    return random.randint(*_SCROLL_PX)


def _url_is_verify(url) -> bool:
    """URL 是否落在验证码/验证中心页(这类可由用户在浏览器里手动通过)。纯函数。"""
    u = (url or "").lower()
    return any(h in u for h in _VERIFY_URL_HINTS)


def _fatal_reason(cookie_names, had_session: bool, consecutive_api_errors: int):
    """纯函数:不可恢复的异常(用户过验证码也救不回,两模式都硬停)。返回原因或 None。"""
    if had_session and not (set(cookie_names) & _SESSION_COOKIES):
        return "登录态丢失"
    if consecutive_api_errors >= 2:
        return "接口连续返回异常(疑似风控)"
    return None


def _profile_dir():
    from ..models_manager import app_data_dir
    return ProfileRegistry(app_data_dir()).resolve("douyin").path


def is_logged_in() -> bool:
    """是否已登录过(标记存在)。登录态实际存在独立浏览器档案里,持久复用。"""
    return (_profile_dir() / _LOGIN_MARK).exists()


def clear_login() -> bool:
    """清除路B 登录态:删除独立 profile 目录(含 cookie/session),回到未登录。

    隐私出口:让用户在 App 内一键抹掉本机残留的抖音登录凭据,不依赖手动找目录。
    返回是否确实删除了东西(目录原本就不存在则返回 False)。
    """
    import shutil

    from ..models_manager import app_data_dir
    registry = ProfileRegistry(app_data_dir())
    resolution = registry.resolve("douyin")
    if resolution.state == "missing":
        return False
    try:
        with registry.acquire("douyin"):
            if not registry._douyin_browser_absent(resolution):
                raise DouyinCollectError("专用浏览器尚未关闭。", code="profile_busy")
            shutil.rmtree(resolution.path)
    except DouyinCollectError:
        raise
    except Exception as exc:
        raise DouyinCollectError("登录档案未能清除，请关闭专用浏览器后重试。",
                                 code=getattr(exc, "code", "profile_busy")) from exc
    return True


def _open_page(browser_path: str | None, *, is_cancelled=None):
    """经共享壳打开抖音独立 profile；保留旧函数签名与 page 接口。"""
    from ..models_manager import app_data_dir

    shell = BrowserShell(ProfileRegistry(app_data_dir()))
    try:
        page = shell.open(BrowserLaunchSpec(
            platform_id="douyin",
            profile_id="default",
            browser_path=browser_path,
        ), is_cancelled=is_cancelled)
        if page is None:
            raise InterruptedError("采集已取消")
        return page
    except BrowserShellError as exc:
        if exc.code == "dependency_unavailable":
            raise DouyinCollectError(
                "需要浏览器驱动组件(DrissionPage)未就绪。请确认已安装依赖。", code=exc.code
            ) from exc
        raise DouyinCollectError("专用浏览器暂时无法使用。", code=exc.code) from exc


def _cookie_names(page) -> set:
    try:
        return {c.get("name") for c in (page.cookies() or [])}
    except Exception:  # noqa: BLE001
        return set()


def _browser_alive(page):
    try:
        return getattr(getattr(page, "states", None), "is_alive", True) is not False
    except Exception:
        return False


def _post_cursor(packet, sec_uid):
    """Only an official target-author response may contribute data or an end."""
    try:
        target = urlsplit(packet.url)
        query = parse_qs(target.query)
        host = (target.hostname or "").lower()
        cursor = query.get("max_cursor", [])
        if (target.scheme == "https" and (host == "douyin.com" or host.endswith(".douyin.com"))
                and target.port in (None, 443) and target.username is None and target.password is None
                and target.path.rstrip("/") == "/aweme/v1/web/aweme/post"
                and query.get("sec_user_id") == [sec_uid]):
            if (len(cursor) != 1 or not 1 <= len(cursor[0]) <= 64
                    or not cursor[0].isascii() or not cursor[0].isdecimal()):
                raise DouyinCollectError("目标作品请求的分页信息无法识别。", code="DOUYIN_PLATFORM_CHANGED")
            return str(int(cursor[0]))
    except (ValueError, AttributeError, TypeError):
        pass
    return None


def _response_payload(response):
    if response.status != 200:
        raise _http_failure(response.status)
    payload = response.body
    if isinstance(payload, (str, bytes)):
        if not payload.strip():
            raise DouyinCollectError("抖音没有返回作品数据。", code="DOUYIN_EMPTY_RESPONSE")
        try:
            payload = json.loads(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            raise DouyinCollectError("抖音返回的作品格式暂时无法识别。", code="DOUYIN_PLATFORM_CHANGED") from exc
    if not isinstance(payload, dict) or type(payload.get("status_code")) is not int:
        raise DouyinCollectError("抖音返回的作品格式暂时无法识别。", code="DOUYIN_PLATFORM_CHANGED")
    if payload["status_code"] != 0:
        raise DouyinCollectError("抖音未返回成功的作品结果。", code="DOUYIN_SERVICE_ERROR")
    return payload


def _login_wall_present(page):
    try:
        path = urlsplit(str(page.url)).path.lower()
        if path.startswith(("/login", "/passport")):
            return True
        for selector in ('css:[data-e2e="login-modal"]', 'css:[data-e2e="login-panel"]',
                         'css:iframe[src*="/passport/"]'):
            if any(element.states.is_displayed for element in page.eles(selector, timeout=.1)):
                return True
    except Exception:
        pass
    return False


def _page_login_marker(page, *, clear=False):
    paths = getattr(page, "paths", None)
    if paths is None:
        return
    marker = paths.resolution.path / _LOGIN_MARK
    try:
        if clear:
            marker.unlink(missing_ok=True)
        else:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(b"ok")
    except OSError:
        pass  # A presence cache cannot change a valid collection result.


def _wait_for_session(page, is_cancelled: CancelCb) -> bool:
    """在有限宽限内确认真实会话；只看 cookie 名，不读值。"""
    for attempt in range(_SESSION_CHECKS):
        if not _browser_alive(page):
            raise DouyinCollectError("专用浏览器已关闭。", code="DOUYIN_BROWSER_CLOSED")
        if _cookie_names(page) & _SESSION_COOKIES:
            return True
        if is_cancelled():
            return False
        if attempt + 1 < _SESSION_CHECKS:
            time.sleep(_SESSION_CHECK_WAIT_S)
    return False


def _has_more_from_body(body: dict) -> bool | None:
    """仅接受 JSON 布尔值或整数 0/1，未知值不冒充终点。"""
    value = body.get("has_more")
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def login(browser_path: str | None = None, progress: ProgressCb | None = None,
          is_cancelled: CancelCb | None = None, timeout_s: int = 300) -> bool:
    """打开独立 profile 浏览器让用户扫码登录(小号)。登录态持久,后续采集复用。

    检测到登录会话(sessionid cookie)即写标记并返回 True;超时/取消返回 False。
    App 全程不经手账号密码,登录由用户在浏览器自行完成。
    """
    progress = progress or (lambda *_: None)
    is_cancelled = is_cancelled or (lambda: False)
    page = _open_page(browser_path)
    try:
        page.get("https://www.douyin.com/")
        progress("请在弹出的浏览器里用小号扫码登录…", 0)
        waited = 0
        while waited < timeout_s and not is_cancelled():
            if not _browser_alive(page):
                progress("专用浏览器已关闭，请重新登录。", 0)
                return False
            names = _cookie_names(page)
            if ("sessionid" in names or "sessionid_ss" in names) and not _login_wall_present(page):
                (_profile_dir() / _LOGIN_MARK).write_text("ok", encoding="utf-8")
                progress("登录成功！后续全量采集会自动复用，无需再登。", 0)
                return True
            time.sleep(2)
            waited += 2
        return False
    finally:
        try:
            page.quit()
        except Exception:  # noqa: BLE001
            pass


def _scroll_step_js(amount: int) -> str:
    """单次"按增量"下滚的 JS:滚窗口 + 滚所有可滚动内层容器(都按 amount 像素,而非瞬跳到底)。"""
    return (
        f"window.scrollBy(0, {amount});"
        "document.querySelectorAll('*').forEach(function(e){"
        f"  if(e.scrollHeight - e.clientHeight > 200){{ try{{e.scrollTop = e.scrollTop + {amount};}}catch(x){{}} }}"
        "});"
        "return document.documentElement.scrollHeight;"
    )


def _human_scroll(page, is_cancelled) -> int:
    """拟人化滚动:分 2~4 小步、每步随机像素、步间微停。返回当前页面高度。"""
    height = 0
    for _ in range(random.randint(*_SCROLL_STEPS)):
        if is_cancelled():
            break
        try:
            height = int(page.run_js(_scroll_step_js(_pick_scroll_amount())) or 0)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_micro_wait())
    return height


def _nudge_last_card(page) -> None:
    """卡住兜底:把最后一个作品卡片滚进视野,逼出下一批懒加载(仅在页面不再长高时才用)。"""
    for sel in ('css:a[href*="/video/"]', 'css:a[href*="modal_id"]'):
        try:
            cards = page.eles(sel, timeout=0.5)
            if cards:
                page.scroll.to_see(cards[-1])
                break
        except Exception:  # noqa: BLE001
            pass


def _has_captcha_element(page) -> bool:
    """页面是否出现【可见的】验证类元素。

    只认真正显示出来、用户能操作的验证容器——避免误判抖音 DOM 里预埋的隐藏容器
    而进入"幽灵等待"(测试实测:撞限流但页面没有可操作验证时,旧逻辑会干等到超时)。
    """
    for sel in _CAPTCHA_SELECTORS:
        try:
            for e in (page.eles(sel, timeout=0.3) or []):
                try:
                    if e.states.is_displayed:
                        return True
                except Exception:  # noqa: BLE001
                    continue   # 拿不到可见性 → 不当作"可操作验证",免得幽灵等待
        except Exception:  # noqa: BLE001
            pass
    return False


def _bring_to_front(page) -> None:
    """把采集浏览器窗口弹到前台并激活,确保用户能看到 / 操作验证码。"""
    for action in (lambda: page.set.window.show(),
                   lambda: page.set.window.normal(),
                   lambda: page.set.activate()):
        try:
            action()
        except Exception:  # noqa: BLE001
            pass


def _scroll_bottom(page) -> int:
    """效率模式:一步滚到底(窗口 + 所有可滚容器),快。返回当前页面高度。"""
    try:
        return int(page.run_js(
            "window.scrollTo(0, document.body.scrollHeight);"
            "document.querySelectorAll('*').forEach(function(e){"
            "  if(e.scrollHeight - e.clientHeight > 200){ try{e.scrollTop = e.scrollHeight;}catch(x){} }"
            "});"
            "return document.documentElement.scrollHeight;") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _captcha_present(page, url) -> bool:
    """是否出现"可由用户手动通过"的验证码(URL 信号 或 页面元素信号)。"""
    return _url_is_verify(url) or _has_captcha_element(page)


def _wait_captcha_cleared(page, is_cancelled, progress, count: int,
                          timeout_s: int = _CAPTCHA_WAIT_S) -> bool:
    """低风险模式:暂停并等用户在浏览器里手动过验证码;过掉返回 True,超时/取消返回 False。

    全程**不自动解、不绕过**——只是轮询页面,等真人通过平台这道闸,再让采集继续。
    """
    deadline = time.monotonic() + max(0, timeout_s)
    while time.monotonic() < deadline and not is_cancelled():
        try:
            cur = page.url
        except Exception:  # noqa: BLE001
            cur = ""
        if not _captcha_present(page, cur):
            return time.monotonic() < deadline
        progress(f"等待你在弹出的浏览器窗口完成验证…完成后自动继续（已采集 {count} 条）。", count)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(2, remaining))
    return False


def _browser_target(profile_url, cancelled):
    from .router import detect_link
    identity = detect_link(profile_url)
    if identity.kind != "homepage":
        identity = _resolve_douyin_link(profile_url, is_cancelled=cancelled)
    if identity.kind == "work":
        return None, identity.extracted_id
    return resolve_sec_uid(identity.canonical_url, is_cancelled=cancelled), None


def _read_browser_work_author(page, work_id, progress, cancelled, *, timeout_s=45,
                              verification_timeout_s=_CAPTCHA_WAIT_S):
    """Use the already-authorized browser to bind this exact work to its author."""
    url = f"https://www.douyin.com/video/{work_id}"
    if cancelled():
        raise InterruptedError("采集已取消")
    page.listen.start(_DETAIL_API)
    progress("正在确认指定作品的作者，再读取该作者主页；可随时取消。", 0)
    if cancelled():
        raise InterruptedError("采集已取消")
    page.get(url)
    deadline = time.monotonic() + timeout_s
    verified = False
    while time.monotonic() < deadline:
        if cancelled():
            raise InterruptedError("采集已取消")
        if not _browser_alive(page):
            raise DouyinCollectError("专用浏览器已关闭。", code="DOUYIN_BROWSER_CLOSED")
        if _login_wall_present(page):
            raise DouyinCollectError("请重新登录后读取这条作品。", code="DOUYIN_LOGIN_REQUIRED")
        if _captcha_present(page, str(page.url)):
            if verified:
                raise DouyinCollectError("作品页再次要求人工验证。", code="DOUYIN_VERIFICATION_REQUIRED")
            verified = True
            _bring_to_front(page)
            progress("请在专用浏览器中自行完成验证，完成后继续确认作品作者；可随时取消。", 0)
            if not _wait_captcha_cleared(page, cancelled, progress, 0, timeout_s=verification_timeout_s):
                if cancelled():
                    raise InterruptedError("采集已取消")
                raise DouyinCollectError("等待人工验证超时。", code="DOUYIN_VERIFICATION_REQUIRED")
            if cancelled():
                raise InterruptedError("采集已取消")
            page.listen.start(_DETAIL_API)
            page.get(url)
            deadline = time.monotonic() + timeout_s
            continue
        packet = page.listen.wait(timeout=.5, raise_err=False)
        if not packet:
            continue
        try:
            target = urlsplit(packet.url)
            host = (target.hostname or "").lower()
            matches = (target.scheme == "https" and (host == "douyin.com" or host.endswith(".douyin.com"))
                       and target.port in (None, 443) and target.username is None and target.password is None
                       and target.path.rstrip("/") == "/" + _DETAIL_API
                       and parse_qs(target.query).get("aweme_id") == [work_id])
        except (AttributeError, TypeError, ValueError):
            matches = False
        if not matches:
            continue
        payload = _response_payload(packet.response)
        try:
            item = parse_aweme_detail(payload, work_id)
        except (DouyinCollectError, TypeError, ValueError, AttributeError) as exc:
            raise DouyinCollectError("作品信息与请求不一致，已停止。", code="DOUYIN_PLATFORM_CHANGED") from exc
        if not _SEC_UID_RE.fullmatch(item.author_id):
            raise DouyinCollectError("作品作者信息无法确认。", code="DOUYIN_PLATFORM_CHANGED")
        return item.author_id
    raise DouyinCollectError("没有读取到指定作品的作者，请提供作者主页后重试。", code="DOUYIN_EMPTY_RESPONSE")


def collect_latest(
    profile_url: str,
    *,
    limit: int = 20,
    progress: ProgressCb | None = None,
    is_cancelled: CancelCb | None = None,
    browser_path: str | None = None,
    login_timeout_s: float = 300,
    timeout_s: float = 45,
    verification_timeout_s: float = _CAPTCHA_WAIT_S,
) -> tuple[list[CollectedItem], str]:
    """Explicit login fallback: read only the author's first page, never scroll."""
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("部分采集范围必须为 1 至 20 条。")
    progress = progress or (lambda *_: None)
    cancelled = is_cancelled or (lambda: False)
    cancellation = "已取消（已采集的已保留）。"
    if cancelled():
        return [], cancellation
    sec_uid, work_id = _browser_target(profile_url, cancelled)
    page = _open_page(browser_path, is_cancelled=cancelled)
    items = []
    try:
        if cancelled():
            return [], cancellation
        page.get("https://www.douyin.com/")
        login_deadline = time.monotonic() + login_timeout_s
        if not (_cookie_names(page) & _SESSION_COOKIES):
            _bring_to_front(page)
            progress("请在 Lulu 专用浏览器中手动登录；登录后只读取最新一页，最多 20 条，可随时取消。", 0)
        while not (_cookie_names(page) & _SESSION_COOKIES):
            if cancelled():
                return [], cancellation
            if getattr(getattr(page, "states", None), "is_alive", True) is False:
                raise DouyinCollectError("专用浏览器已关闭，请重新开始采集。", code="DOUYIN_BROWSER_CLOSED")
            if time.monotonic() >= login_deadline:
                raise DouyinCollectError("未检测到登录，请在专用浏览器自行登录后重试。", code="DOUYIN_LOGIN_REQUIRED")
            time.sleep(0.5)
        if cancelled():
            return [], cancellation

        if work_id is not None:
            sec_uid = _read_browser_work_author(page, work_id, progress, cancelled,
                                               timeout_s=timeout_s, verification_timeout_s=verification_timeout_s)
        url = f"https://www.douyin.com/user/{sec_uid}"
        page.listen.start(_POST_API)
        progress(f"正在读取目标作者最新一页，最多 {limit} 条…", 0)
        if cancelled():
            return [], cancellation
        page.get(url)
        deadline = time.monotonic() + timeout_s
        hint_at = time.monotonic() + min(8.0, timeout_s / 2)
        login_prompted = False
        login_waited = False
        verification_waited = False
        while time.monotonic() < deadline:
            if cancelled():
                return [], cancellation
            if getattr(getattr(page, "states", None), "is_alive", True) is False:
                raise DouyinCollectError("专用浏览器已关闭，请重新开始采集。", code="DOUYIN_BROWSER_CLOSED")
            if _login_wall_present(page):
                if login_waited:
                    raise DouyinCollectError("登录尚未生效，请重新登录。", code="DOUYIN_LOGIN_EXPIRED")
                login_waited = True
                _page_login_marker(page, clear=True)
                _bring_to_front(page)
                progress("登录可能已失效，请在专用浏览器中手动登录；完成后继续读取，可随时取消。", 0)
                login_deadline = time.monotonic() + login_timeout_s
                while _login_wall_present(page) or not (_cookie_names(page) & _SESSION_COOKIES):
                    if cancelled():
                        return [], cancellation
                    if not _browser_alive(page):
                        raise DouyinCollectError("专用浏览器已关闭。", code="DOUYIN_BROWSER_CLOSED")
                    if time.monotonic() >= login_deadline:
                        raise DouyinCollectError("等待重新登录超时。", code="DOUYIN_LOGIN_EXPIRED")
                    time.sleep(.5)
                page.listen.start(_POST_API)
                page.get(url)
                deadline = time.monotonic() + timeout_s
                continue
            if not login_prompted and time.monotonic() >= hint_at:
                login_prompted = True
                _bring_to_front(page)
                progress("暂未读取到作品，请在专用浏览器检查登录是否有效，必要时手动登录；可随时取消。", 0)
            if _captcha_present(page, str(page.url)):
                if verification_waited:
                    raise DouyinCollectError("抖音再次要求人工验证，请重新开始采集。", code="DOUYIN_VERIFICATION_REQUIRED")
                verification_waited = True
                _bring_to_front(page)
                progress("请在专用浏览器中自行完成验证，完成后继续读取最新一页；可随时取消。", 0)
                if not _wait_captcha_cleared(page, cancelled, progress, 0, timeout_s=verification_timeout_s):
                    if cancelled():
                        return [], cancellation
                    raise DouyinCollectError("等待人工验证超时，请重新开始采集。", code="DOUYIN_VERIFICATION_REQUIRED")
                if cancelled():
                    return [], cancellation
                if getattr(getattr(page, "states", None), "is_alive", True) is False:
                    raise DouyinCollectError("专用浏览器已关闭，请重新开始采集。", code="DOUYIN_BROWSER_CLOSED")
                page.listen.start(_POST_API)
                page.get(url)
                deadline = time.monotonic() + timeout_s
                continue
            packet = page.listen.wait(timeout=0.5, raise_err=False)
            if not packet:
                continue
            if _post_cursor(packet, sec_uid) != "0":
                continue
            payload = _response_payload(packet.response)
            items = parse_author_page(payload, sec_uid)[:limit]
            for item in items:
                item.source_method = "profile_latest"
            if cancelled():
                return items, cancellation
            _page_login_marker(page)
            progress(f"已读取目标作者最新一页，共 {len(items)} 条。", len(items))
            return items, ""
        raise DouyinCollectError("抖音暂未返回作品首页，请检查浏览器中的登录或验证状态后重试。", code="DOUYIN_EMPTY_RESPONSE")
    except DouyinCollectError as exc:
        if sec_uid and _SEC_UID_RE.fullmatch(sec_uid):
            exc.resolved_author_id = sec_uid
        if exc.code in {"DOUYIN_LOGIN_REQUIRED", "DOUYIN_LOGIN_EXPIRED"}:
            _page_login_marker(page, clear=True)
        raise
    except InterruptedError:
        raise
    except Exception as exc:
        code = "DOUYIN_NETWORK_ERROR" if _browser_alive(page) else "DOUYIN_BROWSER_CLOSED"
        raise DouyinCollectError("专用浏览器连接已中断。", code=code) from exc
    finally:
        failure_in_flight = sys.exc_info()[0] is not None
        try:
            page.quit()
        except Exception:  # noqa: BLE001 - close only this owned session
            if not failure_in_flight and not cancelled():
                raise DouyinCollectError("专用浏览器未能确认正常关闭。",
                                         code="browser_close_failed", items=items) from None


def collect_all(
    profile_url: str,
    progress: ProgressCb | None = None,
    is_cancelled: CancelCb | None = None,
    browser_path: str | None = None,
    mode: str = MODE_EFFICIENCY,
    idle_rounds_to_stop: int = _IDLE_ROUNDS_TO_STOP,
    max_rounds: int = _MAX_ROUNDS,
) -> tuple[list[CollectedItem], str]:
    """登录态全量采集该作者作品。两种内部模式:

    - 效率(默认):快速一步到底、短等待,尽快采完;撞验证码即停并保留已采集。
    - 低风险:温和分段随机滚动、降频;撞验证码暂停,等用户在浏览器手动过后自动续采,力求采全。

    两模式都:掉登录/接口连续风控 → 硬停;**绝不自动解或绕过验证码**;数据只留本地。
    返回 (items, reason):只有成功响应的 has_more=0 能让 reason 为 "";
    空转、轮数上限和其他异常都保留已采结果并返回诚实原因。
    """
    progress = progress or (lambda *_: None)
    is_cancelled = is_cancelled or (lambda: False)
    low_risk = (mode == MODE_LOW_RISK)
    if is_cancelled():
        return [], "已取消（已采集的已保留）。"
    sec_uid, work_id = _browser_target(profile_url, is_cancelled)
    url = f"https://www.douyin.com/user/{sec_uid}" if sec_uid else "https://www.douyin.com/"
    if is_cancelled():
        return [], "已取消（已采集的已保留）。"
    page = _open_page(browser_path, is_cancelled=is_cancelled)

    seen: dict[str, CollectedItem] = {}
    pages: dict[str, tuple[bool | None, str | None, tuple[str, ...]]] = {}
    anomaly = ""
    failure = None

    def proven_terminal():
        cursor = "0"
        visited = set()
        while cursor in pages and cursor not in visited:
            visited.add(cursor)
            more, following, _work_ids = pages[cursor]
            if more is False:
                return True
            if more is not True or following is None:
                break
            cursor = following
        return False

    try:
        if is_cancelled():
            return [], "已取消（已采集的已保留）。"
        page.listen.start(_POST_API)
        page.get(url)
        progress("已打开作者主页，正在确认登录状态…", 0)

        if not _wait_for_session(page, is_cancelled):
            if is_cancelled():
                return [], "已取消（已采集的已保留）。"
            _page_login_marker(page, clear=True)
            raise DouyinCollectError("登录态丢失，请重新登录后再试。")

        if work_id is not None:
            sec_uid = _read_browser_work_author(page, work_id, progress, is_cancelled)
            url = f"https://www.douyin.com/user/{sec_uid}"
            if is_cancelled():
                return [], "已取消（已采集的已保留）。"
            page.listen.start(_POST_API)
            page.get(url)

        had_session = True
        idle = 0          # 连续「无新增 且 页面没变高」的轮数
        rounds = 0
        last_height = 0
        api_errors = 0    # 连续接口异常计数(疑似风控)
        terminal_confirmed = False
        first_page_deadline = time.monotonic() + _FIRST_PAGE_TIMEOUT_S
        next_long_pause = random.randint(*_LONG_PAUSE_EVERY)
        while rounds < max_rounds and not is_cancelled():
            if not _browser_alive(page):
                raise DouyinCollectError("专用浏览器已关闭。", code="DOUYIN_BROWSER_CLOSED")
            if _login_wall_present(page):
                raise DouyinCollectError("登录已失效，请重新登录。", code="DOUYIN_LOGIN_EXPIRED")
            rounds += 1
            # 滚动:低风险=分段随机(卡住才强拉);效率=一步到底滚两次
            if low_risk:
                height = _human_scroll(page, is_cancelled)
                if not (height > last_height + 10):
                    _nudge_last_card(page)
            else:
                height = _scroll_bottom(page)
                time.sleep(0.9)
                if not is_cancelled():
                    height = max(height, _scroll_bottom(page))
                time.sleep(0.9)

            if is_cancelled():
                break

            new = 0
            pkt = page.listen.wait(timeout=0.5, raise_err=False)
            packet_count = 0
            while pkt and packet_count < _MAX_PACKETS_PER_ROUND and not is_cancelled():
                packet_count += 1
                cursor = _post_cursor(pkt, sec_uid)
                if cursor is not None:
                    body = _response_payload(pkt.response)
                    works = body.get("aweme_list")
                    if not isinstance(works, list):
                        raise DouyinCollectError("作品列表格式已变化。", code="DOUYIN_PLATFORM_CHANGED")
                    # Convert each trusted work immediately. A later bad entry
                    # cannot invalidate an earlier, already verified one.
                    work_ids = []
                    for raw in works:
                        author = raw.get("author") if isinstance(raw, dict) else None
                        author_id = author.get("sec_uid") if isinstance(author, dict) else None
                        if (isinstance(author_id, str) and _SEC_UID_RE.fullmatch(author_id)
                                and author_id != sec_uid):
                            # Real author feeds can contain a foreign work. Do
                            # not use it or this page's end as completeness proof.
                            raise DouyinCollectError("列表混入其他作者的作品，目标作者已读作品保留，但无法确认采全。",
                                                     code="DOUYIN_INCOMPLETE")
                        item = parse_author_page({"status_code": 0, "aweme_list": [raw]}, sec_uid)[0]
                        item.source_method = "profile_all"
                        work_ids.append(item.work_id)
                        if item.work_id not in seen:
                            seen[item.work_id] = item
                            new += 1
                    following = body.get("max_cursor")
                    if type(following) is int and following >= 0:
                        following = str(following)
                    elif not (isinstance(following, str) and 1 <= len(following) <= 64
                              and following.isascii() and following.isdecimal()):
                        following = None
                    if following is not None:
                        following = str(int(following))
                    evidence = (_has_more_from_body(body), following, tuple(work_ids))
                    if cursor in pages and pages[cursor] != evidence:
                        raise DouyinCollectError("同一页返回了不一致的分页或作品，无法确认采全。",
                                                 code="DOUYIN_INCOMPLETE")
                    pages.setdefault(cursor, evidence)
                if not is_cancelled():
                    pkt = page.listen.wait(timeout=0.5, raise_err=False)

            if is_cancelled():
                break
            if not _browser_alive(page):
                raise DouyinCollectError("专用浏览器已关闭。", code="DOUYIN_BROWSER_CLOSED")

            try:
                cur_url = page.url
            except Exception:  # noqa: BLE001
                cur_url = url

            # ① 可人工通过的验证码:低风险=暂停等用户过后续采;效率=停并提示切低风险
            if _captcha_present(page, cur_url):
                if low_risk:
                    _bring_to_front(page)   # 把采集浏览器弹到前台,确保用户看得到/能操作验证
                    progress("检测到验证码：已把采集浏览器弹到前台，请在该窗口完成验证，"
                             f"完成后会自动继续（已采集 {len(seen)} 条）。", len(seen))
                    if not _wait_captcha_cleared(page, is_cancelled, progress, len(seen)):
                        if is_cancelled():
                            break       # 用户取消,走下面的"已取消"分支
                        anomaly = (
                            f"⚠️ 等待验证超时（约 {_CAPTCHA_WAIT_S} 秒未检测到通过）；已采集 {len(seen)} 条已保留。\n"
                            "请确认采集浏览器窗口已在前台并完成验证；若窗口里始终没有可操作的验证，"
                            "多为平台限流（非验证码），建议稍后再试或改用「效率模式」。")
                        progress(anomaly, len(seen))
                        break
                    api_errors = 0
                    idle = 0
                    if not seen:
                        first_page_deadline = time.monotonic() + _FIRST_PAGE_TIMEOUT_S
                    time.sleep(_human_wait())
                    continue          # 过验证后直接进下一轮,避免被误判到底
                anomaly = (
                    f"⚠️ 检测到可见验证，已停止；已采集 {len(seen)} 条已保留。"
                    "请在浏览器完成验证后重新开始。"
                )
                progress(anomaly, len(seen))
                break

            # ② 不可恢复异常:两模式都硬停
            reason = _fatal_reason(_cookie_names(page), had_session, api_errors)
            if reason:
                anomaly = f"⚠️ {reason}，已停止；已采集 {len(seen)} 条已保留。请稍后再试。"
                progress(anomaly, len(seen))
                break

            # 只有成功作品列表包的明确终点才能证明全量完成。
            if proven_terminal():
                if seen:
                    terminal_confirmed = True
                    progress(f"已确认采集到底，共 {len(seen)} 条。", len(seen))
                break

            grew = height > last_height + 10
            last_height = max(last_height, height)

            # 每轮都报进度,让用户看见在动 + 页面高度便于判断滚动是否生效
            if not seen:
                progress("登录状态正常，正在等待作品加载…", 0)
            elif new or grew:
                progress(f"采集中…已采集 {len(seen)} 条（滚动中，可随时取消）", len(seen))
            else:
                progress(f"已采集 {len(seen)} 条，正在确认是否到底 {idle + 1}/{idle_rounds_to_stop}…",
                         len(seen))

            if new or grew:
                idle = 0
            else:
                idle += 1
            # 空转只能证明“暂时没动”，不能证明“已经采全”。
            if seen and idle >= idle_rounds_to_stop:
                anomaly = (
                    f"⚠️ 页面已停止加载新作品，但未能确认已全部采集；"
                    f"已采集 {len(seen)} 条已保留。请稍后重试。"
                )
                progress(anomaly, len(seen))
                break

            if not seen and time.monotonic() >= first_page_deadline:
                raise DouyinCollectError("抖音暂未返回作品首页，目前无法确认主页是否为空。",
                                         code="DOUYIN_EMPTY_RESPONSE")

            # 节奏:低风险才降频(主等待随机 + 偶发长停顿);效率已在滚动里留了 0.9s×2
            if low_risk:
                time.sleep(_human_wait())
                if rounds >= next_long_pause and not is_cancelled():
                    time.sleep(random.uniform(*_LONG_PAUSE))
                    next_long_pause = rounds + random.randint(*_LONG_PAUSE_EVERY)

        if (
            not anomaly
            and not terminal_confirmed
            and seen
            and not is_cancelled()
            and rounds >= max_rounds
        ):
            anomaly = (
                f"⚠️ 已达到本次采集安全上限，未能确认已全部采集；"
                f"已采集 {len(seen)} 条已保留。请稍后重试。"
            )
            progress(anomaly, len(seen))
    except DouyinCollectError as exc:
        if exc.code in {"DOUYIN_LOGIN_REQUIRED", "DOUYIN_LOGIN_EXPIRED"}:
            _page_login_marker(page, clear=True)
        failure = exc
    except InterruptedError:
        if not is_cancelled():
            failure = DouyinCollectError("浏览器连接已中断。", code="DOUYIN_NETWORK_ERROR")
    except Exception as exc:  # Boundary: return verified works even if CDP fails.
        code = "DOUYIN_NETWORK_ERROR" if _browser_alive(page) else "DOUYIN_BROWSER_CLOSED"
        failure = DouyinCollectError("专用浏览器连接已中断。", code=code)
    finally:
        try:
            page.quit()
        except Exception:
            if failure is None:
                failure = DouyinCollectError("专用浏览器未能正常关闭。", code="browser_close_failed")

    items = list(seen.values())
    if is_cancelled():
        return items, "已取消（已采集的已保留）。"
    if failure is not None:
        failure.items = tuple(items)
        raise failure
    if anomaly:
        return items, anomaly
    if is_cancelled():
        return items, "已取消（已采集的已保留）。"
    if not seen:
        raise DouyinCollectError(
            "未取得可读取的作品首页，目前无法确认主页是否为空。\n"
            "请确认作者页在采集浏览器中能正常显示后重试。", code="DOUYIN_EMPTY_RESPONSE"
        )
    if not terminal_confirmed:
        return items, (
            f"⚠️ 未取得全量结束信号；已采集 {len(items)} 条已保留。"
            "请稍后重试。"
        )
    return items, ""
