"""抖音匿名、登录首屏与全量采集链的 adapter 包装。

本模块不复制匿名请求、浏览器滚动、TLS 或节奏实现。默认调用在真正执行
``collect()`` 时才惰性导入旧链，测试可注入 Fake 函数而不启动浏览器或联网。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..contracts import CollectRequest, CollectResult
from ..models import CollectedItem

ProgressCallback = Callable[[str, int], None]
CancelCallback = Callable[[], bool]
FetchLatest = Callable[[str, int], list[CollectedItem]]
CollectAll = Callable[
    [str, ProgressCallback | None, CancelCallback | None, str | None, str | None],
    tuple[list[CollectedItem], str],
]


class CollectLatest(Protocol):
    def __call__(
        self,
        profile_url: str,
        *,
        limit: int = 20,
        progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
        browser_path: str | None = None,
    ) -> tuple[list[CollectedItem], str]: ...


_ERROR_REASONS = {
    "DOUYIN_ACCESS_RESTRICTED": "permission_denied",
    "DOUYIN_LOGIN_REQUIRED": "login_required",
    "DOUYIN_RATE_LIMITED": "rate_limited",
    "DOUYIN_VERIFICATION_REQUIRED": "captcha",
    "DOUYIN_EMPTY_RESPONSE": "no_items_unverified",
    "DOUYIN_EMPTY_LIST": "no_items_unverified",
    "DOUYIN_EMPTY_DETAIL": "no_items_unverified",
    "DOUYIN_PLATFORM_CHANGED": "platform_changed",
    "DOUYIN_NETWORK_ERROR": "network_error",
    "DOUYIN_SERVICE_ERROR": "network_error",
    "DOUYIN_BROWSER_CLOSED": "browser_closed",
    "DOUYIN_INCOMPLETE": "incomplete",
    "DOUYIN_LOGIN_EXPIRED": "login_expired",
    "dependency_unavailable": "browser_unavailable",
    "browser_missing": "browser_unavailable",
    "browser_unverified": "browser_unavailable",
    "browser_launch_failed": "browser_unavailable",
    "browser_close_failed": "browser_unavailable",
    "profile_busy": "profile_busy",
    "browser_port_busy": "profile_busy",
    "DOUYIN_INVALID_PROFILE": "invalid_input",
}
_FAILURE_ACTIONS = {
    "permission_denied": "抖音拒绝了本次访问，请稍后重试。",
    "login_required": "抖音要求登录后读取作品，请在专用浏览器中自行登录。",
    "login_expired": "抖音专用登录已失效，请重新登录后重试。",
    "rate_limited": "抖音限制了本次访问频率，请稍后重试。",
    "captcha": "抖音要求人工验证，请在专用浏览器中自行完成验证。",
    "no_items_unverified": "抖音未返回可读取的作品列表，目前无法确认主页是否为空。",
    "platform_changed": "抖音返回了无法解析的数据,可能是平台改版,请反馈给开发者。",
    "network_error": "连接抖音失败或响应超时，请检查网络后重试。",
    "invalid_input": "无法识别抖音作者主页，请检查主页链接。",
    "incomplete": "本次未能确认已采全，已保留读取到的作品，请稍后重试。",
    "browser_closed": "专用浏览器已关闭，请重新开始采集。",
    "browser_unavailable": "专用浏览器暂时无法使用，请确认浏览器已安装并关闭遗留窗口后重试。",
    "profile_busy": "专用浏览器仍被占用，请先结束另一个采集或登录任务，再关闭遗留浏览器窗口后重试。",
}
_UNEXPECTED_FAILURE_ACTION = "采集过程发生异常，请重试或反馈此问题。"


def _default_fetch_latest(profile_url: str, count: int, *, is_cancelled=None) -> list[CollectedItem]:
    from ..douyin_fetch import fetch_latest

    return fetch_latest(profile_url, count=count, is_cancelled=is_cancelled)


def _default_collect_latest(
    profile_url: str,
    *,
    limit: int = 20,
    progress: ProgressCallback | None = None,
    is_cancelled: CancelCallback | None = None,
    browser_path: str | None = None,
) -> tuple[list[CollectedItem], str]:
    from ..douyin_browser import collect_latest

    return collect_latest(
        profile_url, limit=limit, progress=progress,
        is_cancelled=is_cancelled, browser_path=browser_path,
    )


def _default_collect_all(
    profile_url: str,
    progress: ProgressCallback | None,
    is_cancelled: CancelCallback | None,
    browser_path: str | None,
    browser_mode: str | None,
) -> tuple[list[CollectedItem], str]:
    from ..douyin_browser import collect_all

    kwargs: dict[str, Any] = {
        "progress": progress,
        "is_cancelled": is_cancelled,
        "browser_path": browser_path,
    }
    if browser_mode is not None:
        kwargs["mode"] = browser_mode
    return collect_all(profile_url, **kwargs)


def _reason_code(message: str) -> str:
    """把既有中文异常收敛到 P1A 封闭 reason code，不改旧文案。"""
    text = str(message or "")
    if "取消" in text:
        return "cancelled"
    if "登录态丢失" in text or "登录过期" in text:
        return "login_expired"
    if any(marker in text for marker in ("验证码", "等待验证", "人工验证", "可见验证")):
        return "captcha"
    if any(marker in text for marker in ("未能确认", "安全上限", "未取得全量结束")):
        return "incomplete"
    if "浏览器已关闭" in text:
        return "browser_closed"
    if "风控" in text or "限流" in text:
        return "rate_limited"
    if "权限" in text:
        return "permission_denied"
    if "改版" in text or "无法解析" in text:
        return "platform_changed"
    if "未登录" in text or "登录" in text:
        return "login_required"
    if "链接" in text or "作者主页" in text or "主页链接" in text:
        return "invalid_input"
    if "没有采集到" in text or "没有返回数据" in text:
        return "no_items_unverified"
    return "network_error"


def _bounded(items: list[CollectedItem], limit: int | None) -> list[CollectedItem]:
    return list(items if limit is None else items[:limit])


def _exception_result(exc: Exception) -> CollectResult:
    """Use typed causes first; exception text never becomes public diagnostics."""
    code = getattr(exc, "code", "")
    reason = _ERROR_REASONS.get(code) if isinstance(code, str) else None
    message = str(exc)
    if reason is None:
        reason = _reason_code(message)
    items = tuple(getattr(exc, "items", ()))
    if reason == "cancelled":
        return CollectResult.cancelled(items)
    action = _FAILURE_ACTIONS[reason]
    # Old collectors only supplied Chinese strings. Their known network wording
    # remains compatible, while unexpected exceptions do not blame connectivity.
    if code == "DOUYIN_BROWSER_CLOSED":
        action = "专用浏览器已关闭，请重新开始采集。"
    elif code == "DOUYIN_SERVICE_ERROR":
        action = "抖音服务暂时无法返回作品，请稍后重试。"
    elif code == "DOUYIN_EMPTY_DETAIL":
        action = "抖音暂未返回这条作品的详情，请改用作者主页链接或稍后重试。"
    elif (reason == "network_error" and code != "DOUYIN_NETWORK_ERROR"
            and not any(marker in message for marker in ("网络", "超时"))):
        action = _UNEXPECTED_FAILURE_ACTION
    if items:
        return CollectResult.partial(items, reason_code=reason, next_action=action)
    return CollectResult.failed(reason, next_action=action)


@dataclass
class DouyinAdapter:
    """统一抖音 adapter；依赖注入只用于隔离测试和未来受控替换。"""

    fetch_latest_fn: FetchLatest = _default_fetch_latest
    collect_all_fn: CollectAll = _default_collect_all
    collect_latest_fn: CollectLatest = field(default=_default_collect_latest, kw_only=True)

    platform: str = "douyin"
    modes: tuple[str, ...] = ("anonymous_latest", "profile_latest", "profile_all")

    def collect(
        self,
        request: CollectRequest,
        *,
        progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
        browser_path: str | None = None,
        browser_mode: str | None = None,
    ) -> CollectResult:
        """执行旧链并映射为统一结果；已有条目永不因后续异常被清空。"""
        if request.identity.platform != self.platform:
            return CollectResult.failed(
                "unsupported_link", next_action="请选择抖音作者主页后重试。"
            )
        if request.mode not in self.modes:
            return CollectResult.failed(
                "unsupported_link", next_action=f"抖音暂不支持采集模式：{request.mode}"
            )
        if request.mode in {"profile_latest", "profile_all"} and request.profile_id != "default":
            return CollectResult.failed(
                "invalid_input", next_action="抖音现有登录链仅兼容 default profile。"
            )

        cancelled = is_cancelled or (lambda: False)
        if cancelled():
            return CollectResult.cancelled()
        profile_url = request.identity.canonical_url
        if not profile_url:
            return CollectResult.failed("invalid_input", next_action="缺少抖音主页链接。")

        if request.mode == "anonymous_latest":
            return self._collect_anonymous(request, profile_url, cancelled)
        if request.mode == "profile_latest":
            return self._collect_latest(
                request, profile_url, progress=progress,
                is_cancelled=cancelled, browser_path=browser_path,
            )
        return self._collect_profile(
            request,
            profile_url,
            progress=progress,
            is_cancelled=cancelled,
            browser_path=browser_path,
            browser_mode=browser_mode,
        )

    def _collect_anonymous(
        self,
        request: CollectRequest,
        profile_url: str,
        is_cancelled: CancelCallback,
    ) -> CollectResult:
        try:
            if self.fetch_latest_fn is _default_fetch_latest:
                raw_items = self.fetch_latest_fn(profile_url, request.limit or 20, is_cancelled=is_cancelled)
            else:
                raw_items = self.fetch_latest_fn(profile_url, request.limit or 20)
            items = _bounded(raw_items, request.limit)
        except Exception as exc:  # noqa: BLE001 - 旧链异常统一收敛
            if is_cancelled():
                return CollectResult.cancelled(getattr(exc, "items", ()))
            return _exception_result(exc)
        if is_cancelled():
            return CollectResult.cancelled(items)
        if not items:
            return CollectResult.failed(
                "no_items_unverified",
                next_action="抖音未返回作品，无法确认主页真实为空，请稍后重试。",
            )
        return CollectResult.complete(items)

    def _collect_latest(
        self,
        request: CollectRequest,
        profile_url: str,
        *,
        progress: ProgressCallback | None,
        is_cancelled: CancelCallback,
        browser_path: str | None,
    ) -> CollectResult:
        limit = 20 if request.limit is None else max(1, min(20, request.limit))
        try:
            raw_items, reason = self.collect_latest_fn(
                profile_url, limit=limit, progress=progress,
                is_cancelled=is_cancelled, browser_path=browser_path,
            )
            items = _bounded(raw_items, limit)
        except Exception as exc:  # noqa: BLE001 - 浏览器边界只返回稳定脱敏原因
            if is_cancelled():
                return CollectResult.cancelled()
            return _exception_result(exc)

        if is_cancelled() or _reason_code(reason) == "cancelled":
            return CollectResult.cancelled(items)
        if reason:
            failure = _exception_result(RuntimeError(reason))
            if items:
                return CollectResult.partial(
                    items, reason_code=failure.reason_code,
                    next_action=failure.next_action,
                )
            return failure
        if not items:
            return CollectResult.failed(
                "no_items_unverified",
                next_action="没有读取到作品，目前无法确认主页真实为空，请稍后重试。",
            )
        return CollectResult.complete(items)

    def _collect_profile(
        self,
        request: CollectRequest,
        profile_url: str,
        *,
        progress: ProgressCallback | None,
        is_cancelled: CancelCallback,
        browser_path: str | None,
        browser_mode: str | None,
    ) -> CollectResult:
        try:
            raw_items, legacy_reason = self.collect_all_fn(
                profile_url, progress, is_cancelled, browser_path, browser_mode
            )
            items = _bounded(raw_items, request.limit)
        except Exception as exc:  # noqa: BLE001 - 旧链异常统一收敛
            if is_cancelled():
                return CollectResult.cancelled(getattr(exc, "items", ()))
            return _exception_result(exc)

        if is_cancelled() or _reason_code(legacy_reason) == "cancelled":
            return CollectResult.cancelled(items)
        if legacy_reason:
            code = _reason_code(legacy_reason)
            if items:
                return CollectResult.partial(
                    items,
                    reason_code=code,
                    next_action=legacy_reason,
                )
            return CollectResult.failed(code, next_action=legacy_reason)
        if not items:
            return CollectResult.failed(
                "no_items_unverified",
                next_action="没有采集到作品，无法确认主页真实为空，请检查登录态后重试。",
            )
        return CollectResult.complete(items)


__all__ = ["DouyinAdapter"]
