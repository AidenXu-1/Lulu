"""路 B:登录态全量采集 —— 驱动用户本机浏览器滚动作者主页,截获 aweme/post 取全量。

ADR-0013 opt-in 例外:用户显式选择 + 自行在浏览器登录(建议小号)+ 独立 profile 隔离
主浏览器 + App 不经手凭据 + 数据只留本地。匿名默认路在 douyin_fetch(本模块不碰)。

依赖 DrissionPage(驱动本机 Chrome,不内置 Chromium);仅本路使用,惰性导入。
"""

from __future__ import annotations

import random
import time
from typing import Callable

from .browser_shell import BrowserLaunchSpec, BrowserShell, BrowserShellError
from .models import CollectedItem, dedupe, from_aweme
from .douyin_fetch import resolve_sec_uid, DouyinCollectError
from .profiles import ProfileRegistry

ProgressCb = Callable[[str, int], None]
CancelCb = Callable[[], bool]

_POST_API = "aweme/v1/web/aweme/post"
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

    pdir = _profile_dir()
    if not pdir.exists():
        return False
    shutil.rmtree(pdir, ignore_errors=True)
    return not pdir.exists()


def _open_page(browser_path: str | None):
    """经共享壳打开抖音独立 profile；保留旧函数签名与 page 接口。"""
    from ..models_manager import app_data_dir

    shell = BrowserShell(ProfileRegistry(app_data_dir()))
    try:
        return shell.open(BrowserLaunchSpec(
            platform_id="douyin",
            profile_id="default",
            browser_path=browser_path,
        ))
    except BrowserShellError as exc:
        if exc.code == "dependency_unavailable":
            raise DouyinCollectError(
                "需要浏览器驱动组件(DrissionPage)未就绪。请确认已安装依赖。"
            ) from exc
        raise DouyinCollectError(f"打不开浏览器:{exc}\n请确认本机已安装所选浏览器。") from exc


def _cookie_names(page) -> set:
    try:
        ck = page.cookies(as_dict=True) or {}
        return set(ck.keys())
    except Exception:  # noqa: BLE001
        try:
            return {c.get("name") for c in (page.cookies() or [])}
        except Exception:  # noqa: BLE001
            return set()


def _wait_for_session(page, is_cancelled: CancelCb) -> bool:
    """在有限宽限内确认真实会话；只看 cookie 名，不读值。"""
    for attempt in range(_SESSION_CHECKS):
        if _cookie_names(page) & _SESSION_COOKIES:
            return True
        if is_cancelled():
            return False
        if attempt + 1 < _SESSION_CHECKS:
            time.sleep(_SESSION_CHECK_WAIT_S)
    return False


def _clear_stale_login_mark() -> None:
    """只清除已被真实 cookie 反证的 UI 缓存，不删 profile/cookie。"""
    try:
        (_profile_dir() / _LOGIN_MARK).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - 缓存清理失败不得覆盖真实登录结果
        pass


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
            names = _cookie_names(page)
            if "sessionid" in names or "sessionid_ss" in names:
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
    waited = 0
    while waited < timeout_s and not is_cancelled():
        try:
            cur = page.url
        except Exception:  # noqa: BLE001
            cur = ""
        if not _captcha_present(page, cur):
            return True
        progress(f"等待你在弹出的浏览器窗口完成验证…完成后自动继续（已采集 {count} 条）。", count)
        time.sleep(2)
        waited += 2
    return False


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

    sec_uid = resolve_sec_uid(profile_url)
    url = f"https://www.douyin.com/user/{sec_uid}"
    page = _open_page(browser_path)   # 独立 profile,登录态持久、与主浏览器隔离

    seen: dict[str, dict] = {}
    anomaly = ""
    try:
        page.listen.start(_POST_API)
        page.get(url)
        progress("已打开作者主页，正在确认登录状态…", 0)

        if not _wait_for_session(page, is_cancelled):
            if is_cancelled():
                return [], "已取消（已采集的已保留）。"
            _clear_stale_login_mark()
            raise DouyinCollectError("登录态丢失，请重新登录后再试。")

        had_session = True
        idle = 0          # 连续「无新增 且 页面没变高」的轮数
        rounds = 0
        last_height = 0
        api_errors = 0    # 连续接口异常计数(疑似风控)
        terminal_confirmed = False
        next_long_pause = random.randint(*_LONG_PAUSE_EVERY)
        while rounds < max_rounds and not is_cancelled():
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
            round_has_more: bool | None = None
            pkt = page.listen.wait(timeout=5)
            packet_count = 0
            while pkt and packet_count < _MAX_PACKETS_PER_ROUND and not is_cancelled():
                packet_count += 1
                try:
                    resp = pkt.response
                    body = resp.body
                    status = getattr(resp, "status", None)
                    if isinstance(body, dict):
                        sc = body.get("status_code", 0)
                        response_ok = not (
                            (status is not None and status != 200)
                            or (sc not in (0, None))
                        )
                        if not response_ok:
                            api_errors += 1
                        else:
                            api_errors = 0
                            parsed_has_more = _has_more_from_body(body)
                            if parsed_has_more is not None:
                                round_has_more = parsed_has_more
                        for a in (body.get("aweme_list") or []):
                            aid = a.get("aweme_id")
                            if aid and aid not in seen:
                                seen[aid] = a
                                new += 1
                except Exception:  # noqa: BLE001
                    pass
                if not is_cancelled():
                    pkt = page.listen.wait(timeout=2)

            if is_cancelled():
                break

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
            if round_has_more is False:
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

            if not seen and rounds >= idle_rounds_to_stop:
                break

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
    finally:
        try:
            page.quit()
        except Exception:  # noqa: BLE001
            pass

    items = dedupe([from_aweme(a) for a in seen.values()])
    if anomaly:
        return items, anomaly
    if is_cancelled():
        return items, "已取消（已采集的已保留）。"
    if not seen:
        raise DouyinCollectError(
            "没有采集到作品。可能该作者无公开作品，或平台页面暂时无法读取。\n"
            "请确认作者页在采集浏览器中能正常显示后重试。"
        )
    if not terminal_confirmed:
        return items, (
            f"⚠️ 未取得全量结束信号；已采集 {len(items)} 条已保留。"
            "请稍后重试。"
        )
    return items, ""
