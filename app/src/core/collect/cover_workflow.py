"""主页采集封面的纯 Python 共享工作流。

本层只编排已经通过安全门的封面本地化服务：冻结任务偏好、限制并发、
结算取消/失败，并把 R0 的恢复收据映射回采集记录。它不读取受管目录，
不解析或清理恢复 sidecar，也不持有任何网络实现。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
import re
from typing import Protocol
from urllib.parse import urlsplit

from .contracts import CollectResult
from .cover import COVER_FAILURE_CODES, CoverReceipt
from .models import CollectedItem
from .preferences import CollectCoverPreferenceSnapshot


_DOWNLOAD_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_DOWNLOAD_EXTENSIONS = frozenset({".jpg", ".png", ".webp"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_RECOVERY_BLOCK_CODES = frozenset(
    {"cover_busy", "cover_commit_uncertain", "cover_path_blocked"}
)


class CoverLocalizer(Protocol):
    startup_recovery_receipts: Sequence[CoverReceipt]

    def localize(
        self,
        *,
        platform: str,
        work_id: str,
        url: str,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> CoverReceipt: ...


@dataclass(frozen=True)
class CoverItemOutcome:
    """单条封面结算，不含远端 URL、Cookie 或本地绝对路径。"""

    stable_key: str
    status: str
    reason_code: str = ""
    relative_path: str = ""


@dataclass(frozen=True)
class CoverWorkflowResult:
    """保留原采集完成语义，只替换封面字段的工作流结果。"""

    collect_result: CollectResult
    outcomes: tuple[CoverItemOutcome, ...]


@dataclass(frozen=True)
class CoverRecoveryResult:
    """持久化记录经过 R0 恢复入口后的安全展示结果。"""

    items: tuple[CollectedItem, ...]
    outcomes: tuple[CoverItemOutcome, ...]


@dataclass(frozen=True)
class _SettledItem:
    item: CollectedItem
    outcome: CoverItemOutcome


class CoverWorkflowService:
    """封面 C0 + R1 编排；每个实例固定最多两个并发下载。"""

    MAX_CONCURRENCY = 2

    def __init__(self, localizer: CoverLocalizer) -> None:
        if not callable(getattr(localizer, "localize", None)):
            raise TypeError("localizer 必须提供 localize")
        self._localizer = localizer
        receipts = getattr(localizer, "startup_recovery_receipts", ())
        self.startup_recovery_receipts = tuple(receipts)

    def run(
        self,
        result: CollectResult,
        *,
        snapshot: CollectCoverPreferenceSnapshot,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> CoverWorkflowResult:
        """按任务启动快照结算封面，永不改变采集主结果的完成语义。"""

        if not isinstance(snapshot, CollectCoverPreferenceSnapshot):
            raise TypeError("snapshot 必须是 CollectCoverPreferenceSnapshot")
        cancel = is_cancelled or (lambda: False)
        original_items = tuple(result.items)
        settled: list[_SettledItem | None] = [None] * len(original_items)
        eligible: list[int] = []

        for index, item in enumerate(original_items):
            initial = self._initial_settlement(item)
            if initial is not None:
                settled[index] = initial
                continue
            if not snapshot.include_cover:
                settled[index] = self._not_requested(item)
                continue
            if cancel():
                settled[index] = self._url_only(item, cancelled=True)
                continue
            eligible.append(index)

        if snapshot.include_cover and eligible:
            self._run_bounded(
                original_items,
                eligible,
                settled,
                is_cancelled=cancel,
            )

        final: list[_SettledItem] = []
        for index, current in enumerate(settled):
            if current is None:
                current = self._url_only(original_items[index], cancelled=True)
            final.append(current)
        updated_result = replace(
            result,
            items=tuple(entry.item for entry in final),
        )
        return CoverWorkflowResult(
            collect_result=updated_result,
            outcomes=tuple(entry.outcome for entry in final),
        )

    def reconcile_persisted(
        self,
        items: Sequence[CollectedItem],
    ) -> CoverRecoveryResult:
        """用 R0 首次操作入口覆盖不可信的持久化 downloaded 展示。

        空 URL 会在 R0 完成本目标恢复检查后失败关闭，因此此入口不解析
        sidecar、不做 DNS，也不会开始新下载。
        """

        updated: list[CollectedItem] = []
        outcomes: list[CoverItemOutcome] = []
        for item in items:
            if not self._has_identity(item):
                updated.append(item)
                outcomes.append(self._outcome(item, item.cover_status))
                continue
            try:
                receipt = self._localizer.localize(
                    platform=item.platform,
                    work_id=item.work_id,
                    url="",
                    is_cancelled=lambda: False,
                )
            except Exception:
                receipt = CoverReceipt.failed("cover_commit_uncertain")

            if receipt.status == "recovered_previous":
                settled = self._accept_success(item, receipt)
                if settled is None:
                    settled = self._recovery_blocked(
                        item, "cover_commit_uncertain"
                    )
                else:
                    settled = _SettledItem(
                        settled.item,
                        replace(settled.outcome, status="recovered_previous"),
                    )
            elif (
                receipt.status == "failed"
                and receipt.reason_code == "cover_invalid_url"
            ):
                settled = _SettledItem(
                    item, self._outcome(item, item.cover_status)
                )
            elif (
                receipt.status == "failed"
                and receipt.reason_code in _RECOVERY_BLOCK_CODES
            ):
                settled = self._recovery_blocked(item, receipt.reason_code)
            else:
                settled = self._recovery_blocked(
                    item, "cover_commit_uncertain"
                )
            updated.append(settled.item)
            outcomes.append(settled.outcome)
        return CoverRecoveryResult(tuple(updated), tuple(outcomes))

    def _run_bounded(
        self,
        items: tuple[CollectedItem, ...],
        indices: list[int],
        settled: list[_SettledItem | None],
        *,
        is_cancelled: Callable[[], bool],
    ) -> None:
        pending = iter(indices)
        running: dict[Future[_SettledItem], int] = {}

        def submit_next(executor: ThreadPoolExecutor) -> bool:
            if is_cancelled():
                return False
            try:
                index = next(pending)
            except StopIteration:
                return False
            future = executor.submit(
                self._localize_one, items[index], is_cancelled
            )
            running[future] = index
            return True

        with ThreadPoolExecutor(
            max_workers=self.MAX_CONCURRENCY,
            thread_name_prefix="lulu-cover",
        ) as executor:
            for _slot in range(self.MAX_CONCURRENCY):
                if not submit_next(executor):
                    break
            while running:
                done, _ = wait(tuple(running), return_when=FIRST_COMPLETED)
                for future in done:
                    index = running.pop(future)
                    try:
                        settled[index] = future.result()
                    except Exception:
                        settled[index] = self._failed(
                            items[index], "cover_commit_failed"
                        )
                while (
                    len(running) < self.MAX_CONCURRENCY
                    and not is_cancelled()
                    and submit_next(executor)
                ):
                    pass

        for index in indices:
            if settled[index] is None:
                settled[index] = self._url_only(items[index], cancelled=True)

    def _localize_one(
        self,
        item: CollectedItem,
        is_cancelled: Callable[[], bool],
    ) -> _SettledItem:
        if is_cancelled():
            return self._url_only(item, cancelled=True)
        try:
            receipt = self._localizer.localize(
                platform=item.platform,
                work_id=item.work_id,
                url=item.cover_url,
                is_cancelled=is_cancelled,
            )
        except Exception:
            return self._failed(item, "cover_commit_failed")

        if receipt.status in {"downloaded", "recovered_previous"}:
            success = self._accept_success(item, receipt)
            if success is not None:
                if receipt.status == "recovered_previous":
                    return _SettledItem(
                        success.item,
                        replace(success.outcome, status="recovered_previous"),
                    )
                return success
            return self._failed(item, "cover_commit_failed")
        if (
            receipt.status == "failed"
            and receipt.reason_code == "cover_cancelled"
        ):
            return self._url_only(item, cancelled=True)
        reason = (
            receipt.reason_code
            if receipt.status == "failed"
            and receipt.reason_code in COVER_FAILURE_CODES
            else "cover_commit_failed"
        )
        return self._failed(item, reason)

    def _initial_settlement(
        self, item: CollectedItem
    ) -> _SettledItem | None:
        if not item.cover_url:
            clean = replace(
                item,
                cover_local_path="",
                cover_status="not_requested",
            )
            return _SettledItem(
                clean, self._outcome(clean, "not_requested")
            )
        if not self._has_identity(item) or not self._is_https(item.cover_url):
            return self._url_only(item)
        return None

    @staticmethod
    def _has_identity(item: CollectedItem) -> bool:
        return bool(
            _PLATFORM_RE.fullmatch(str(item.platform or ""))
            and str(item.work_id or "").strip()
        )

    @staticmethod
    def _is_https(url: str) -> bool:
        try:
            parsed = urlsplit(str(url))
            return bool(
                parsed.scheme.casefold() == "https"
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            return False

    @staticmethod
    def _valid_receipt_path(platform: str, value: str) -> bool:
        if (
            not isinstance(value, str)
            or not value
            or "\\" in value
            or value.split("/") != [platform, value.rsplit("/", 1)[-1]]
        ):
            return False
        path = PurePosixPath(value)
        return bool(
            not path.is_absolute()
            and len(path.parts) == 2
            and path.parts[0] == platform
            and path.parts[1] not in {"", ".", ".."}
            and path.suffix.casefold() in _DOWNLOAD_EXTENSIONS
        )

    def _accept_success(
        self, item: CollectedItem, receipt: CoverReceipt
    ) -> _SettledItem | None:
        if (
            receipt.reason_code
            or not self._valid_receipt_path(
                item.platform, receipt.relative_path
            )
            or isinstance(receipt.size, bool)
            or not isinstance(receipt.size, int)
            or receipt.size <= 0
            or not isinstance(receipt.sha256, str)
            or not _SHA256_RE.fullmatch(receipt.sha256)
            or not isinstance(receipt.mime_type, str)
            or receipt.mime_type not in _DOWNLOAD_MIMES
        ):
            return None
        clean = replace(
            item,
            cover_local_path=receipt.relative_path,
            cover_status="downloaded",
            last_error_code=self._clear_cover_error(item),
        )
        return _SettledItem(
            clean,
            self._outcome(
                clean, "downloaded", relative_path=receipt.relative_path
            ),
        )

    def _recovery_blocked(
        self, item: CollectedItem, reason_code: str
    ) -> _SettledItem:
        blocked = replace(
            item,
            cover_local_path="",
            cover_status="failed",
            last_error_code=self._cover_error(item, reason_code),
        )
        return _SettledItem(
            blocked, self._outcome(blocked, "failed", reason_code)
        )

    def _failed(
        self, item: CollectedItem, reason_code: str
    ) -> _SettledItem:
        failed = replace(
            item,
            cover_local_path="",
            cover_status="failed",
            last_error_code=self._cover_error(item, reason_code),
        )
        return _SettledItem(
            failed, self._outcome(failed, "failed", reason_code)
        )

    def _url_only(
        self, item: CollectedItem, *, cancelled: bool = False
    ) -> _SettledItem:
        clean = replace(
            item,
            cover_local_path="",
            cover_status="url_only" if item.cover_url else "not_requested",
        )
        return _SettledItem(
            clean,
            self._outcome(
                clean,
                clean.cover_status,
                "cover_cancelled" if cancelled and item.cover_url else "",
            ),
        )

    @staticmethod
    def _cover_error(item: CollectedItem, reason_code: str) -> str:
        if item.last_error_code and not item.last_error_code.startswith("cover:"):
            return item.last_error_code
        return f"cover:{reason_code}"

    @staticmethod
    def _clear_cover_error(item: CollectedItem) -> str:
        return (
            ""
            if item.last_error_code.startswith("cover:")
            else item.last_error_code
        )

    def _not_requested(self, item: CollectedItem) -> _SettledItem:
        clean = replace(
            item,
            cover_local_path="",
            cover_status="not_requested",
        )
        return _SettledItem(
            clean, self._outcome(clean, "not_requested")
        )

    @staticmethod
    def _outcome(
        item: CollectedItem,
        status: str,
        reason_code: str = "",
        *,
        relative_path: str = "",
    ) -> CoverItemOutcome:
        return CoverItemOutcome(
            stable_key=item.stable_key,
            status=status,
            reason_code=reason_code,
            relative_path=relative_path,
        )
