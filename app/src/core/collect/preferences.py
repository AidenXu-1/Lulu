"""素材下载偏好的进程内单一真相服务。

主页采集与工作台共享同一份三元组。只有配置原子保存成功后才
广播完整快照；保存失败会恢复整个旧三元组与封面合同标记。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Protocol

try:  # package import: src.core.collect.preferences
    from ...config import COLLECT_COVER_CONTRACT_VERSION
except ImportError:  # focused tests may expose app/src as the import root
    from config import COLLECT_COVER_CONTRACT_VERSION

MATERIAL_PREFERENCE_VERSION = 1
DOWNLOAD_MODES = frozenset({"audio", "video"})


class MaterialPreferenceConfig(Protocol):
    link_media_mode: str
    keep_media: bool
    collect_cover: bool
    collect_cover_contract_version: int

    def save(self) -> bool: ...


@dataclass(frozen=True)
class MaterialPreferenceSnapshot:
    """任务创建时冻结的不可变素材偏好。"""

    download_mode: str = "audio"
    save_media: bool = False
    save_cover: bool = False
    version: int = MATERIAL_PREFERENCE_VERSION

    def __post_init__(self) -> None:
        if self.version != MATERIAL_PREFERENCE_VERSION:
            raise ValueError("不支持的素材偏好版本")
        if self.download_mode not in DOWNLOAD_MODES:
            raise ValueError("下载内容必须是 audio 或 video")
        if not isinstance(self.save_media, bool) or not isinstance(
            self.save_cover, bool
        ):
            raise TypeError("素材保存偏好必须是 bool")

    @property
    def include_cover(self) -> bool:
        """与 0.2.5 封面工作流兼容的只读名称。"""

        return self.save_cover


@dataclass(frozen=True)
class MaterialPreferenceReceipt:
    success: bool
    code: str
    previous_snapshot: MaterialPreferenceSnapshot
    current_snapshot: MaterialPreferenceSnapshot
    changed: bool
    notification_failures: int = 0


class MaterialPreferenceService:
    """对配置中三项素材偏好进行一次保存、完整广播。"""

    def __init__(self, config: MaterialPreferenceConfig) -> None:
        self._config = config
        self._lock = RLock()
        self._subscribers: list[Callable[[MaterialPreferenceSnapshot], None]] = []
        # 构造阶段只验证，不修改或保存配置。
        self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> MaterialPreferenceSnapshot:
        mode = str(getattr(self._config, "link_media_mode", "audio") or "")
        if mode not in DOWNLOAD_MODES:
            mode = "audio"
        keep = getattr(self._config, "keep_media", False)
        if not isinstance(keep, bool):
            keep = False
        raw_cover = getattr(self._config, "collect_cover", False)
        marker = getattr(self._config, "collect_cover_contract_version", 0)
        cover = bool(
            isinstance(raw_cover, bool)
            and raw_cover
            and not isinstance(marker, bool)
            and marker == COLLECT_COVER_CONTRACT_VERSION
        )
        return MaterialPreferenceSnapshot(mode, keep, cover)

    def snapshot(self) -> MaterialPreferenceSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def subscribe(
        self, callback: Callable[[MaterialPreferenceSnapshot], None]
    ) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("callback 必须可调用")
        with self._lock:
            self._subscribers.append(callback)
        active = True

        def unsubscribe() -> None:
            nonlocal active
            with self._lock:
                if not active:
                    return
                active = False
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def set(
        self,
        *,
        download_mode: str | None = None,
        save_media: bool | None = None,
        save_cover: bool | None = None,
    ) -> MaterialPreferenceReceipt:
        if download_mode is not None and download_mode not in DOWNLOAD_MODES:
            raise ValueError("下载内容必须是 audio 或 video")
        for value in (save_media, save_cover):
            if value is not None and not isinstance(value, bool):
                raise TypeError("素材保存偏好必须是 bool")

        with self._lock:
            previous_raw = (
                getattr(self._config, "link_media_mode", "audio"),
                getattr(self._config, "keep_media", False),
                getattr(self._config, "collect_cover", False),
                getattr(self._config, "collect_cover_contract_version", 0),
            )
            previous = self._snapshot_unlocked()
            target = MaterialPreferenceSnapshot(
                download_mode or previous.download_mode,
                previous.save_media if save_media is None else save_media,
                previous.save_cover if save_cover is None else save_cover,
            )
            target_marker = (
                COLLECT_COVER_CONTRACT_VERSION
                if target.save_cover
                else previous_raw[3]
            )
            target_raw = (
                target.download_mode,
                target.save_media,
                target.save_cover,
                target_marker,
            )
            if target == previous and target_raw == previous_raw:
                return MaterialPreferenceReceipt(
                    True, "unchanged", previous, previous, False
                )

            (
                self._config.link_media_mode,
                self._config.keep_media,
                self._config.collect_cover,
                self._config.collect_cover_contract_version,
            ) = target_raw
            try:
                saved = self._config.save() is True
            except Exception:
                saved = False
            if not saved:
                (
                    self._config.link_media_mode,
                    self._config.keep_media,
                    self._config.collect_cover,
                    self._config.collect_cover_contract_version,
                ) = previous_raw
                return MaterialPreferenceReceipt(
                    False, "config_save_failed", previous, previous, False
                )
            subscribers = tuple(self._subscribers)

        failures = 0
        for callback in subscribers:
            try:
                callback(target)
            except Exception:
                failures += 1
        return MaterialPreferenceReceipt(
            True, "saved", previous, target, target != previous, failures
        )


# 0.2.5 的封面工作流仍使用下列类型。它们仅是兼容面，生产 UI
# 只创建 MaterialPreferenceService，不会产生第二份偏好真相。
@dataclass(frozen=True)
class CollectCoverPreferenceSnapshot:
    include_cover: bool


@dataclass(frozen=True)
class CollectCoverPreferenceReceipt:
    success: bool
    code: str
    previous_value: bool
    current_value: bool
    changed: bool
    notification_failures: int = 0


class CollectCoverPreferenceService:
    """0.2.5 API 兼容适配器；内部仍使用完整偏好服务。"""

    def __init__(self, config: MaterialPreferenceConfig) -> None:
        self._materials = MaterialPreferenceService(config)
        self._subscriptions: list[Callable[[], None]] = []

    def value(self) -> bool:
        return self._materials.snapshot().save_cover

    def snapshot(self) -> CollectCoverPreferenceSnapshot:
        return CollectCoverPreferenceSnapshot(self.value())

    def subscribe(self, callback: Callable[[bool], None]) -> Callable[[], None]:
        unsubscribe = self._materials.subscribe(
            lambda snapshot: callback(snapshot.save_cover)
        )
        self._subscriptions.append(unsubscribe)
        return unsubscribe

    def set(self, enabled: bool) -> CollectCoverPreferenceReceipt:
        if not isinstance(enabled, bool):
            raise TypeError("封面偏好必须是 bool")
        receipt = self._materials.set(save_cover=enabled)
        return CollectCoverPreferenceReceipt(
            receipt.success,
            receipt.code,
            receipt.previous_snapshot.save_cover,
            receipt.current_snapshot.save_cover,
            receipt.changed,
            receipt.notification_failures,
        )
