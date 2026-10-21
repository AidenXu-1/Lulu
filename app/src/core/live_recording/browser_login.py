"""Explicit live-login bridge from Lulu's owned Douyin browser to secure storage.

Called only by the user's login action. No ordinary-browser discovery, cookie
logging, secret persistence or credential reads occur in this module.
"""
import re
import time
from urllib.parse import urlsplit

from ..collect.browser_shell import BrowserLaunchSpec, BrowserShell, BrowserShellError
from ..collect.profiles import ProfileRegistry
from .contracts import LiveError

_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_SESSION = {'sessionid', 'sessionid_ss'}


def cookie_header(cookies, now=None):
    """Accept only valid, current Douyin cookies; never leak rejected values."""
    now = time.time() if now is None else now
    values = {}
    for cookie in cookies:
        domain = str(cookie.get('domain', '')).lower().lstrip('.')
        if domain not in {'douyin.com', 'www.douyin.com', 'live.douyin.com'}:
            continue
        expires = cookie.get('expires', -1)
        if isinstance(expires, (float, int)) and expires > 0 and expires <= now:
            continue
        name, value = cookie.get('name'), cookie.get('value')
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or not isinstance(value, str) or not value
                or any(ord(c) < 33 or ord(c) > 126 or c == ';' for c in value)):
            continue
        values[name] = value
    if not (_SESSION & values.keys()):
        return ''
    header = '; '.join(f'{name}={value}' for name, value in values.items())
    if len(header) > 16384:
        raise LiveError('登录信息过长，请使用高级方式，或重新登录后再试')
    return header


def login_cookie(app_data, *, is_cancelled=lambda: False, progress=lambda text: None,
                 timeout=300, shell=None):
    if is_cancelled():
        return ''
    shell = shell or BrowserShell(ProfileRegistry(app_data))
    page = None
    try:
        # Same owned identity and leases as homepage collection. Contention is
        # rejected by BrowserShell; no second process attaches to a busy profile.
        page = shell.open(BrowserLaunchSpec(platform_id='douyin'), is_cancelled=is_cancelled)
        if page is None:
            return ''
        page.close_timeout = .5
        page.set.timeouts(base=2, page_load=10, script=2)
        page.set.retry_times(0)
        page.set.load_mode.eager()
        progress('请在弹出的浏览器里登录自己的抖音账号。')
        page.get('https://www.douyin.com/')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not is_cancelled():
            host = urlsplit(page.url).hostname
            if host in {'douyin.com', 'www.douyin.com', 'live.douyin.com'}:
                header = cookie_header(page.cookies(all_domains=False, all_info=True))
                if header and not is_cancelled():
                    return header
            for _ in range(10):
                if is_cancelled():
                    return ''
                time.sleep(.1)
        if is_cancelled():
            return ''
        raise LiveError('登录等待已结束，原登录信息保留；需要时可重新登录')
    except BrowserShellError as exc:
        if exc.code == 'profile_busy':
            raise LiveError('抖音专用浏览器正在使用，请结束主页采集后重试') from None
        if exc.code == 'dependency_unavailable':
            raise LiveError('浏览器组件未就绪，请重新打开 Lulu 后重试') from None
        message = {'browser_missing': '请先安装 Google Chrome，或手动填写 Cookie。',
                   'browser_unverified': 'Chrome 当前无法用于登录，请更新或修复 Chrome，或手动填写 Cookie。'}.get(exc.code,
                   '登录窗口未能打开，请重试或手动填写 Cookie；原登录信息保留')
        raise LiveError(message) from None
    except LiveError:
        raise
    except Exception:
        if is_cancelled():
            return ''
        raise LiveError('浏览器登录未完成，原登录信息保留；请重试或使用高级方式') from None
    finally:
        if page is not None:
            try:
                page.quit()
            except Exception:
                pass  # Owned shell still releases only its own leases.
