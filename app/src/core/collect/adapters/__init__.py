"""采集 adapter 注册表。

只解析已经通过能力门的平台。当前开放抖音和小红书核心 adapter，其他平台
继续依据 capability 失败关闭。
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import stat
import sys
from typing import Any

from ..contracts import AdapterCapability, adapter_capability

_BUILD_PROFILE_BYTES = 4096


class AdapterUnavailableError(LookupError):
    """请求的平台当前没有可用 adapter。"""

    def __init__(self, platform: str, reason: str) -> None:
        self.platform = platform
        self.reason = reason
        super().__init__(f"{platform} adapter 不可用: {reason}")


def _sealed_build_profile() -> str:
    """只读取冻结包内的构建身份；源码态不接受环境变量伪装。"""
    if not getattr(sys, "frozen", False):
        return "source"
    root = Path(str(getattr(sys, "_MEIPASS", "")))
    if not root.is_absolute():
        return "invalid"
    path = root / "build_profile.json"
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _BUILD_PROFILE_BYTES
        ):
            return "invalid"
        raw = path.read_bytes()
        if len(raw) != metadata.st_size:
            return "invalid"
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - 构建身份异常必须失败关闭
        return "invalid"
    if not isinstance(payload, dict):
        return "invalid"
    profile = payload.get("profile")
    return (
        profile
        if profile in {"internal_test", "trusted_test", "external_candidate"}
        else "invalid"
    )


def homepage_collection_available(platform: str) -> bool:
    """返回当前构建封存的主页采集能力，不读取用户配置。"""
    key = str(platform or "").strip().lower()
    if key != "xiaohongshu":
        return adapter_capability(key).available
    profile = _sealed_build_profile()
    if profile in {"source", "internal_test", "external_candidate"}:
        return True
    if profile != "trusted_test":
        return False
    try:
        root_package = __package__.split(".", 1)[0]
        module_name = (
            f"{root_package}.release_features"
            if root_package == "src"
            else "release_features"
        )
        release_features = importlib.import_module(module_name)
        return bool(release_features.homepage_collection_enabled(key))
    except Exception:  # noqa: BLE001 - 清单缺失或损坏时失败关闭
        return False


def release_adapter_capability(platform: str) -> AdapterCapability:
    """在核心能力表外叠加 build-sealed 发布门。"""
    capability = adapter_capability(platform)
    if capability.platform == "xiaohongshu" and not homepage_collection_available(
        "xiaohongshu"
    ):
        return AdapterCapability(
            platform="xiaohongshu",
            available=False,
            reason="release_disabled",
        )
    return capability


def resolve_adapter(platform: str) -> Any:
    """按 P1A 能力表解析 adapter，未登记能力一律失败关闭。"""
    key = str(platform or "").strip().lower()
    capability = release_adapter_capability(key)
    if not capability.available:
        raise AdapterUnavailableError(key or "unknown", capability.reason)
    if key == "douyin":
        from .douyin import DouyinAdapter

        return DouyinAdapter()
    if key == "xiaohongshu":
        from .xiaohongshu import XiaohongshuAdapter

        return XiaohongshuAdapter()
    raise AdapterUnavailableError(key or "unknown", capability.reason)


def __getattr__(name: str) -> Any:
    """兼容既有公开导入，同时避免关闭包在注册表导入时加载 XHS 模块。"""
    if name == "DouyinAdapter":
        from .douyin import DouyinAdapter

        return DouyinAdapter
    if name == "XiaohongshuAdapter":
        from .xiaohongshu import XiaohongshuAdapter

        return XiaohongshuAdapter
    raise AttributeError(name)


__all__ = [
    "AdapterUnavailableError",
    "DouyinAdapter",
    "XiaohongshuAdapter",
    "homepage_collection_available",
    "release_adapter_capability",
    "resolve_adapter",
]
