"""公开 API 被拒绝时，使用正常渲染的匿名作品页；不借用登录档案。"""
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from .browser_shell import BrowserShell, BrowserLaunchSpec, _default_options_factory
from .profiles import ProfileRegistry
from .douyin_fetch import DouyinCollectError, parse_aweme_detail, _WORK_ID_RE


def _anonymous_options():
    options = _default_options_factory()
    options.headless(True)
    options.set_load_mode("eager")
    options.set_retry(times=0, interval=0)
    options.set_timeouts(base=1, page_load=8, script=2)
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if chrome.is_file():
        options.set_browser_path(str(chrome))
    return options


def fetch_rendered_item(work_id, progress, *, timeout=18, shell_factory=None):
    if not isinstance(work_id, str) or not _WORK_ID_RE.fullmatch(work_id):
        raise DouyinCollectError("作品身份无效。")
    progress("正在通过匿名网页获取公开作品…", -1.0)
    with tempfile.TemporaryDirectory(prefix="lulu-public-page-") as temporary:
        factory = shell_factory or (lambda root: BrowserShell(
            ProfileRegistry(root), options_factory=_anonymous_options))
        shell = factory(Path(temporary))
        with shell.open(BrowserLaunchSpec(platform_id="douyin", profile_id="public-" + uuid4().hex[:16])) as page:
            page.close_timeout = 1.0
            page.listen.start("aweme/v1/web/aweme/detail/")
            page.get(f"https://www.douyin.com/video/{work_id}")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                progress("等待公开作品页面返回视频…", -1.0)
                if any(marker in str(page.url).lower() for marker in ("/verify", "/captcha", "/login")):
                    raise DouyinCollectError("抖音要求人工验证，请在主页采集的浏览器中处理后重试。", code="DOUYIN_VERIFICATION_REQUIRED")
                packet = page.listen.wait(timeout=0.5, raise_err=False)
                if not packet:
                    continue
                payload = packet.response.body
                if not isinstance(payload, dict):
                    continue
                detail = payload.get("aweme_detail")
                if not isinstance(detail, dict) or str(detail.get("aweme_id", "")) != work_id:
                    continue
                return parse_aweme_detail(payload, work_id)
    raise DouyinCollectError("公开网页也没有返回这条作品的视频；可能需要人工验证，请在主页采集中查看该作品。", code="DOUYIN_PUBLIC_PAGE_UNAVAILABLE")
