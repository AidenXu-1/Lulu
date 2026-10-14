"""小红书专用 profile 的公开主页采集 adapter。

生产接线通过 ``open_session_fn`` 注入已由 P0 验证的常驻专用浏览器会话。
adapter 结束时只 detach 本次 CDP session，不关闭 Chrome；本模块不导入
浏览器库、不扫描 profile、不联网下载。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import re
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from ..contracts import CollectRequest, CollectResult
from ..models import CollectedItem, FIELD_STATE_PREFIX, TRUTHFUL_NUMERIC_FIELDS


MAX_ITEMS = 20
MAX_ROUNDS = 2
_XHS_HOSTS = frozenset({"xiaohongshu.com", "www.xiaohongshu.com"})
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_CONTENT_KINDS = frozenset({"video", "image", "mixed", "unknown"})
_EXTRA_STAT_KEYS = frozenset({"view", "favorite", "coin", "danmaku"})
_MAX_PUBLIC_TIMESTAMP = 4_102_444_800


class PageSession(Protocol):
    """常驻浏览器 task session 的最小 attach/detach 接口。"""

    def get(self, url: str) -> Any: ...

    def run_js(self, script: str) -> Any: ...

    def detach(self) -> None: ...


OpenSession = Callable[[str], PageSession]
ProgressCallback = Callable[[str, int], None]
CancelCallback = Callable[[], bool]


# URL 在浏览器 DOM 内第一次提取时就只保留 origin + pathname。Python 侧仍做
# 第二道白名单校验，防止 Fake、平台结构变化或未来接线把瞬态查询串带入模型。
_SNAPSHOT_SCRIPT = r"""
return (() => {
  const cleanText = value => String(value || '').trim().replace(/\s+/g, ' ').slice(0, 500);
  const safeUrl = value => {
    try {
      const parsed = new URL(String(value || ''), location.href);
      if (parsed.protocol !== 'https:') return '';
      return parsed.origin + parsed.pathname;
    } catch (_) {
      return '';
    }
  };
  const body = String(document.body && document.body.innerText || '').slice(0, 12000);
  const lowerUrl = String(location.href || '').toLowerCase();
  let status = 'ok';
  if (/captcha|verify|security/.test(lowerUrl) || /安全验证|请完成验证|验证码/.test(body)) {
    status = 'captcha';
  } else if (/登录已过期|登录状态已失效|重新登录/.test(body)) {
    status = 'login_expired';
  } else if (/访问频繁|操作频繁|请求频繁|稍后再试/.test(body)) {
    status = 'rate_limited';
  } else if (/\/login(?:\/|$)/.test(location.pathname) || /请先登录|登录后查看/.test(body)) {
    status = 'login_required';
  }
  if (status !== 'ok') {
    return {status, items: [], has_more: false};
  }

  const rows = [];
  const profilePrefix = location.pathname.replace(/\/$/, '') + '/';
  const authorName = document.title.replace(/\s*-\s*小红书.*$/, '').trim();
  for (const anchor of document.querySelectorAll('a[href]')) {
    const parsed = new URL(anchor.href, location.href);
    const canonical = parsed.pathname.includes('/explore/') || parsed.pathname.includes('/discovery/item/');
    const profileChild = parsed.pathname.startsWith(profilePrefix)
      && parsed.pathname.slice(profilePrefix.length).split('/').filter(Boolean).length === 1;
    if (!canonical && !profileChild) continue;
    const workId = parsed.pathname.replace(/\/$/, '').split('/').pop() || '';
    const card = anchor.closest('section') || anchor.parentElement || anchor;
    const image = card.querySelector('img') || anchor.querySelector('img');
    rows.push({
      work_id: cleanText(workId),
      work_url: safeUrl(location.origin + '/explore/' + workId),
      author_name: cleanText(authorName),
      title: cleanText(card.innerText || anchor.innerText || anchor.getAttribute('aria-label')),
      cover_url: image ? safeUrl(image.currentSrc || image.src) : '',
      content_kind: 'unknown'
    });
  }

  const explicitEmpty = /还没有发布任何内容|暂无公开笔记/.test(body)
    && /笔记\s*[·・]?\s*0/.test(body);
  if (status === 'ok' && explicitEmpty) status = 'confirmed_empty';
  return {
    status,
    items: rows,
    has_more: rows.length > 0
      && document.documentElement.scrollHeight > window.scrollY + window.innerHeight + 4
  };
})()
"""

_ADVANCE_SCRIPT = (
    "window.scrollBy(0, Math.max(600, Math.floor(window.innerHeight * 0.8)));"
)


def _https_url(raw: object, *, hosts: frozenset[str] | None = None) -> str:
    """返回无 query/fragment/userinfo 的 HTTPS URL；不保留任何瞬态值。"""
    try:
        parsed = urlsplit(str(raw or ""))
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if parsed.scheme.lower() != "https" or not host:
        return ""
    if parsed.username or parsed.password or (port is not None and port != 443):
        return ""
    if hosts is not None and host not in hosts:
        return ""
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit(("https", netloc, parsed.path or "/", "", ""))


def _safe_id(raw: object) -> str:
    value = str(raw or "").split("?", 1)[0].split("#", 1)[0].rstrip("/").split("/")[-1]
    return value if _SAFE_ID.fullmatch(value) else ""


def _work_identity(raw_id: object, raw_url: object) -> tuple[str, str]:
    """work_id 优先；缺失时才从无查询串公开 URL 回退。"""
    explicit_id = _safe_id(raw_id)
    normalized = _https_url(raw_url, hosts=_XHS_HOSTS)
    url_id = ""
    if normalized:
        parsed = urlsplit(normalized)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2 and parts[0] == "explore":
            url_id = _safe_id(parts[1])
        elif len(parts) == 3 and parts[:2] == ["discovery", "item"]:
            url_id = _safe_id(parts[2])
        elif len(parts) == 4 and parts[:2] == ["user", "profile"]:
            url_id = _safe_id(parts[3])
    if explicit_id and url_id and explicit_id != url_id:
        return "", ""
    work_id = explicit_id or url_id
    if not work_id:
        return "", ""
    return work_id, f"https://www.xiaohongshu.com/explore/{work_id}"


def _profile_url(raw: object) -> str:
    normalized = _https_url(raw, hosts=_XHS_HOSTS)
    if not normalized:
        return ""
    parts = [part for part in urlsplit(normalized).path.split("/") if part]
    if len(parts) != 3 or parts[:2] != ["user", "profile"] or not _safe_id(parts[2]):
        return ""
    return normalized


def _integer(raw: object) -> int:
    if isinstance(raw, bool):
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _truthful_numeric(row: Mapping[str, object], name: str) -> tuple[int, str]:
    """只接受来源字段中的非负整数；缺失或非法值保持 unknown。"""
    if name not in row:
        return 0, "unknown"
    raw = row[name]
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0, "unknown"
    if name == "create_time" and raw > _MAX_PUBLIC_TIMESTAMP:
        return 0, "unknown"
    return raw, "known"


def _public_text(raw: object, limit: int = 500) -> str:
    return str(raw or "").strip()[:limit]


def _item_from_row(row: Mapping[str, object], homepage_url: str) -> CollectedItem | None:
    work_id, work_url = _work_identity(row.get("work_id"), row.get("work_url"))
    if not work_id:
        return None
    content_kind = _public_text(row.get("content_kind"), 20).lower()
    if content_kind not in _CONTENT_KINDS:
        content_kind = "unknown"
    cover_url = _https_url(row.get("cover_url"))
    raw_extra = row.get("extra_stats")
    extra_stats: dict[str, int | str] = {}
    if isinstance(raw_extra, Mapping):
        for key in _EXTRA_STAT_KEYS:
            if key in raw_extra:
                extra_stats[key] = _integer(raw_extra[key])
    values: dict[str, int] = {}
    for name in TRUTHFUL_NUMERIC_FIELDS:
        if name == "duration_ms" and content_kind == "image":
            value, state = 0, "not_applicable"
        else:
            value, state = _truthful_numeric(row, name)
        values[name] = value
        extra_stats[f"{FIELD_STATE_PREFIX}{name}"] = state
    return CollectedItem(
        aweme_id="",
        platform="xiaohongshu",
        work_id=work_id,
        author_id=_safe_id(row.get("author_id")),
        author_name=_public_text(row.get("author_name"), 160),
        homepage_url=homepage_url,
        work_url=work_url,
        title=_public_text(row.get("title")),
        create_time=values["create_time"],
        duration_ms=values["duration_ms"],
        like=values["like"],
        collect=values["collect"],
        comment=values["comment"],
        share=values["share"],
        extra_stats=extra_stats,
        content_kind=content_kind,
        cover_url=cover_url,
        cover_status="url_only" if cover_url else "not_requested",
        source_method="dedicated_profile",
    )


def _next_action(reason_code: str) -> str:
    return {
        "login_required": "请先在 Lulu 专用小红书浏览器中登录测试小号。",
        "login_expired": "专用小红书登录已过期，请重新登录后再试。",
        "captcha": "小红书要求安全验证，请人工处理后重新发起；Lulu 不会绕过验证。",
        "rate_limited": "小红书访问频繁，已停止并保留结果，请稍后再试。",
        "platform_changed": "小红书页面结构已变化，已停止，请更新 Lulu 后重试。",
        "network_error": "浏览器页面读取失败，已停止并保留已有结果，请稍后重试。",
        "no_items_unverified": "没有读取到公开作品，也无法确认主页真实为空，请检查登录态后重试。",
    }[reason_code]


@dataclass
class XiaohongshuAdapter:
    """最多两轮、20 条的小红书公开主页 adapter。"""

    open_session_fn: OpenSession | None = None
    platform: str = "xiaohongshu"
    modes: tuple[str, ...] = ("profile_public",)

    def collect(
        self,
        request: CollectRequest,
        *,
        progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> CollectResult:
        cancelled = is_cancelled or (lambda: False)
        if request.identity.platform != self.platform:
            return CollectResult.failed("unsupported_link", next_action="请选择小红书公开主页后重试。")
        if request.identity.kind != "homepage" or request.mode not in self.modes:
            return CollectResult.failed("unsupported_link", next_action="小红书当前只支持公开主页采集。")
        if request.profile_id != "default":
            return CollectResult.failed("invalid_input", next_action="小红书当前只支持 default 专用 profile。")
        homepage_url = _profile_url(request.identity.canonical_url)
        if not homepage_url:
            return CollectResult.failed("invalid_input", next_action="缺少有效的小红书公开主页链接。")
        if cancelled():
            return CollectResult.cancelled()
        if self.open_session_fn is None:
            return CollectResult.failed("login_required", next_action=_next_action("login_required"))

        limit = min(request.limit or MAX_ITEMS, MAX_ITEMS)
        session: PageSession | None = None
        items: list[CollectedItem] = []
        seen: set[str] = set()
        result: CollectResult | None = None
        try:
            session = self.open_session_fn(request.profile_id)
            if progress is not None:
                progress("正在打开小红书公开主页", 0)
            session.get(homepage_url)
            for round_number in range(1, MAX_ROUNDS + 1):
                if cancelled():
                    result = CollectResult.cancelled(items)
                    break
                snapshot = session.run_js(_SNAPSHOT_SCRIPT)
                if not isinstance(snapshot, Mapping):
                    result = self._stopped(items, "platform_changed", round_number)
                    break
                status = _public_text(snapshot.get("status"), 40).lower() or "ok"
                reason_code = {
                    "login_required": "login_required",
                    "login_expired": "login_expired",
                    "captcha": "captcha",
                    "verification_required": "captcha",
                    "rate_limited": "rate_limited",
                    "platform_changed": "platform_changed",
                }.get(status)
                if reason_code:
                    result = self._stopped(items, reason_code, round_number)
                    break
                if status not in {"ok", "confirmed_empty"}:
                    result = self._stopped(items, "platform_changed", round_number)
                    break
                raw_rows = snapshot.get("items", ())
                if not isinstance(raw_rows, (list, tuple)):
                    result = self._stopped(items, "platform_changed", round_number)
                    break
                normalized_count = 0
                for raw_row in raw_rows:
                    if not isinstance(raw_row, Mapping):
                        continue
                    item = _item_from_row(raw_row, homepage_url)
                    if item is None:
                        continue
                    normalized_count += 1
                    dedupe_key = item.stable_key or item.work_url
                    if not dedupe_key or dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    items.append(item)
                    if len(items) >= limit:
                        break
                if raw_rows and normalized_count == 0:
                    result = self._stopped(items, "platform_changed", round_number)
                    break
                if progress is not None:
                    progress(f"小红书公开主页第 {round_number} 轮", len(items))
                if cancelled():
                    result = CollectResult.cancelled(items)
                    break
                if status == "confirmed_empty":
                    result = (
                        self._stopped(items, "platform_changed", round_number)
                        if items else CollectResult.complete((), confirmed_empty=True)
                    )
                    break
                if len(items) >= limit:
                    result = CollectResult.complete(items)
                    break

                has_more = snapshot.get("has_more") is True
                if not has_more:
                    result = (
                        CollectResult.complete(items)
                        if items else CollectResult.failed(
                            "no_items_unverified", next_action=_next_action("no_items_unverified")
                        )
                    )
                    break
                if round_number == MAX_ROUNDS:
                    result = (
                        CollectResult.complete(items)
                        if items else CollectResult.failed(
                            "no_items_unverified", next_action=_next_action("no_items_unverified")
                        )
                    )
                    break
                session.run_js(_ADVANCE_SCRIPT)
        except Exception:  # noqa: BLE001 - 浏览器异常不得泄漏实现或敏感 URL
            result = (
                CollectResult.cancelled(items)
                if cancelled()
                else self._stopped(items, "network_error", min(MAX_ROUNDS, 1 + bool(items)))
            )
        finally:
            if session is not None:
                try:
                    detach = getattr(session, "detach", None)
                    if callable(detach):
                        detach()
                    elif not callable(getattr(session, "close", None)):
                        raise RuntimeError("session lacks detach contract")
                    # close-only 会话仍由 application service 关闭；BrowserSession
                    # 自己提供 detach()，会释放本会话持有的两把锁。
                except Exception:  # noqa: BLE001 - detach 异常仍需稳定返回合同
                    if result is None or result.completion == "complete":
                        result = self._stopped(items, "network_error", MAX_ROUNDS)
        return result or CollectResult.failed("network_error", next_action=_next_action("network_error"))

    @staticmethod
    def _stopped(items: list[CollectedItem], reason_code: str, round_number: int) -> CollectResult:
        action = _next_action(reason_code)
        if items:
            return CollectResult.partial(
                items,
                reason_code=reason_code,
                next_action=action,
                cursor={"round": max(1, min(round_number, MAX_ROUNDS)), "item_count": len(items)},
            )
        return CollectResult.failed(reason_code, next_action=action)


__all__ = ["MAX_ITEMS", "MAX_ROUNDS", "PageSession", "XiaohongshuAdapter"]
