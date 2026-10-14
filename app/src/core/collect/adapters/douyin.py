"""既有抖音两条采集链的 P1A adapter 包装。

本模块不复制匿名请求、浏览器滚动、TLS 或节奏实现。默认调用在真正执行
``collect()`` 时才惰性导入旧链，测试可注入 Fake 函数而不启动浏览器或联网。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..contracts import CollectRequest, CollectResult
from ..models import CollectedItem

ProgressCallback = Callable[[str, int], None]
CancelCallback = Callable[[], bool]
FetchLatest = Callable[[str, int], list[CollectedItem]]
CollectAll = Callable[
    [str, ProgressCallback | None, CancelCallback | None, str | None, str | None],
    tuple[list[CollectedItem], str],
]


def _default_fetch_latest(profile_url: str, count: int) -> list[CollectedItem]:
    from ..douyin_fetch import fetch_latest

    return fetch_latest(profile_url, count=count)


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
    if "验证码" in text or "等待验证" in text:
        return "captcha"
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


@dataclass
class DouyinAdapter:
    """统一抖音 adapter；依赖注入只用于隔离测试和未来受控替换。"""

    fetch_latest_fn: FetchLatest = _default_fetch_latest
    collect_all_fn: CollectAll = _default_collect_all

    platform: str = "douyin"
    modes: tuple[str, ...] = ("anonymous_latest", "profile_all")

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
        if request.mode == "profile_all" and request.profile_id != "default":
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
            items = _bounded(self.fetch_latest_fn(profile_url, request.limit or 20), request.limit)
        except Exception as exc:  # noqa: BLE001 - 旧链异常统一收敛
            message = str(exc)
            return CollectResult.failed(_reason_code(message), next_action=message)
        if is_cancelled():
            return CollectResult.cancelled(items)
        if not items:
            return CollectResult.failed(
                "no_items_unverified",
                next_action="抖音未返回作品，无法确认主页真实为空，请稍后重试。",
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
            message = str(exc)
            return CollectResult.failed(_reason_code(message), next_action=message)

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
