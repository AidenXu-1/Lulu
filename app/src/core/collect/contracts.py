"""三平台采集的纯数据合同与能力登记。

本模块不导入 adapter、浏览器或网络库。抖音和小红书登记已通过能力门的
核心 adapter；B站继续失败关闭并表达不可用原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping

from .models import CollectedItem

PLATFORMS = frozenset({"douyin", "xiaohongshu", "bilibili"})
LINK_KINDS = frozenset({"homepage", "work", "unknown"})
CONFIDENCES = frozenset({"exact", "probable", "unknown"})
COMPLETIONS = frozenset({"complete", "partial", "cancelled", "failed"})
REASON_CODES = frozenset({
    "invalid_input",
    "ambiguous_link",
    "unsupported_link",
    "login_required",
    "login_expired",
    "captcha",
    "rate_limited",
    "permission_denied",
    "platform_changed",
    "network_error",
    "no_items_unverified",
    "cancelled",
})


@dataclass(frozen=True)
class LinkIdentity:
    """Router 的无网络识别结果。"""

    platform: str = "unknown"
    kind: str = "unknown"
    canonical_url: str = ""
    extracted_id: str = ""
    confidence: str = "unknown"
    reason_code: str = ""

    def __post_init__(self) -> None:
        if self.platform not in PLATFORMS | {"unknown"}:
            raise ValueError(f"未知平台: {self.platform}")
        if self.kind not in LINK_KINDS:
            raise ValueError(f"未知链接类型: {self.kind}")
        if self.confidence not in CONFIDENCES:
            raise ValueError(f"未知置信度: {self.confidence}")
        if self.reason_code and self.reason_code not in REASON_CODES:
            raise ValueError(f"未知 reason code: {self.reason_code}")


@dataclass(frozen=True)
class CollectRequest:
    """平台 adapter 的统一输入。"""

    identity: LinkIdentity
    mode: str
    limit: int | None = None
    include_cover: bool = False
    include_media: bool = False
    profile_id: str = "default"

    def __post_init__(self) -> None:
        if self.identity.platform == "unknown":
            raise ValueError("未知平台不能创建采集请求")
        if not isinstance(self.mode, str) or not self.mode.strip():
            raise ValueError("采集模式不能为空")
        if self.limit is not None and (
            not isinstance(self.limit, int) or isinstance(self.limit, bool) or self.limit <= 0
        ):
            raise ValueError("limit 必须是正整数或 None")
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("profile_id 不能为空")


@dataclass(frozen=True)
class CollectResult:
    """平台 adapter 的统一输出,在构造时强制失败/部分结果不变量。"""

    items: tuple[CollectedItem, ...] = field(default_factory=tuple)
    completion: str = "failed"
    reason_code: str = ""
    next_action: str = ""
    cursor: Mapping[str, object] | None = None
    confirmed_empty: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        if self.cursor is not None:
            object.__setattr__(self, "cursor", MappingProxyType(dict(self.cursor)))
        if self.completion not in COMPLETIONS:
            raise ValueError(f"未知 completion: {self.completion}")
        if self.reason_code and self.reason_code not in REASON_CODES:
            raise ValueError(f"未知 reason code: {self.reason_code}")

        if self.completion == "complete":
            if self.reason_code or self.next_action:
                raise ValueError("complete 不得携带失败原因或下一步")
            if not self.items and not self.confirmed_empty:
                raise ValueError("空列表只有在上游明确证明真实为空时才能 complete")
            return

        if self.confirmed_empty:
            raise ValueError("confirmed_empty 只适用于 complete")
        if not self.reason_code:
            raise ValueError(f"{self.completion} 必须携带 reason_code")
        if self.reason_code == "cancelled" and self.completion != "cancelled":
            raise ValueError("cancelled reason code 只能用于 cancelled")
        if self.completion == "partial":
            if not self.items:
                raise ValueError("partial 必须保留至少一条结果")
            if not self.next_action.strip():
                raise ValueError("partial 必须提供可执行的 next_action")
        elif self.completion == "failed" and self.items:
            raise ValueError("已有结果必须表达为 partial,不能表达为 failed")
        elif self.completion == "cancelled" and self.reason_code != "cancelled":
            raise ValueError("cancelled 必须使用 cancelled reason code")

    @classmethod
    def complete(
        cls,
        items: Iterable[CollectedItem],
        *,
        confirmed_empty: bool = False,
    ) -> CollectResult:
        return cls(items=tuple(items), completion="complete", confirmed_empty=confirmed_empty)

    @classmethod
    def partial(
        cls,
        items: Iterable[CollectedItem],
        *,
        reason_code: str,
        next_action: str,
        cursor: Mapping[str, object] | None = None,
    ) -> CollectResult:
        return cls(
            items=tuple(items),
            completion="partial",
            reason_code=reason_code,
            next_action=next_action,
            cursor=cursor,
        )

    @classmethod
    def cancelled(cls, items: Iterable[CollectedItem] = ()) -> CollectResult:
        return cls(items=tuple(items), completion="cancelled", reason_code="cancelled")

    @classmethod
    def failed(cls, reason_code: str, *, next_action: str = "") -> CollectResult:
        return cls(completion="failed", reason_code=reason_code, next_action=next_action)


@dataclass(frozen=True)
class AdapterCapability:
    platform: str
    available: bool
    reason: str
    modes: tuple[str, ...] = ()


ADAPTER_CAPABILITIES: Mapping[str, AdapterCapability] = MappingProxyType({
    "douyin": AdapterCapability(
        platform="douyin",
        available=True,
        reason="existing_adapter_ready",
        modes=("anonymous_latest", "profile_all"),
    ),
    "xiaohongshu": AdapterCapability(
        platform="xiaohongshu",
        available=True,
        reason="formal_adapter_ready",
        modes=("profile_public",),
    ),
    "bilibili": AdapterCapability(
        platform="bilibili",
        available=False,
        reason="official_homepage_contract_unverified",
    ),
})


def adapter_capability(platform: str) -> AdapterCapability:
    """返回平台能力;未知平台保持 unavailable,不猜 adapter。"""
    key = str(platform or "").strip().lower()
    return ADAPTER_CAPABILITIES.get(
        key,
        AdapterCapability(platform=key or "unknown", available=False, reason="unsupported_platform"),
    )
