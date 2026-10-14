"""小红书常驻浏览器的 task-only 采集监督层。

本模块只拥有单次 task worker 和 collection lease。取消会永久关闭结果提交闸门，
优先请求 worker 自行 detach，超时后只终止 task worker；Chrome、helper、Profile、
动态 CDP 与 controller lease 始终不进入终止或清理集合。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any
from uuid import uuid4

from .contracts import CollectResult
from .models import CollectedItem
from .profiles import ProfileError, ProfileRegistry
from .xhs_browser import XhsBrowserIdentity
from .xhs_process_worker import MAX_FRAME_BYTES, SCHEMA_VERSION, task_worker_main


_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_BROWSER_PATH = re.compile(r"/devtools/browser/[A-Za-z0-9._-]{1,200}\Z")


class XhsProcessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CleanupPolicy:
    graceful_seconds: float = 0.5
    terminate_seconds: float = 0.75
    total_seconds: float = 2.8

    def __post_init__(self) -> None:
        values = (
            self.graceful_seconds,
            self.terminate_seconds,
            self.total_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
            for value in values
        ):
            raise ValueError("cleanup policy 必须为正数")
        if (
            self.graceful_seconds + self.terminate_seconds
            >= self.total_seconds
            or self.total_seconds > 3.0
        ):
            raise ValueError("task-only 取消策略必须在 3 秒内保留强制结束窗口")


@dataclass(frozen=True)
class XhsTerminalReceipt:
    request_id: str
    terminal: str
    task_worker_dead: bool
    result_gate_closed: bool
    cdp_session_detached: bool
    collection_lease_released: bool
    browser_disposition: str

    def to_frame(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": self.request_id,
            "terminal": self.terminal,
            "task_worker_dead": self.task_worker_dead,
            "result_gate_closed": self.result_gate_closed,
            "cdp_session_detached": self.cdp_session_detached,
            "collection_lease_released": self.collection_lease_released,
            "browser_disposition": self.browser_disposition,
        }

    @classmethod
    def from_frame(cls, frame: Mapping[str, Any]) -> XhsTerminalReceipt:
        expected = {
            "schema_version",
            "request_id",
            "terminal",
            "task_worker_dead",
            "result_gate_closed",
            "cdp_session_detached",
            "collection_lease_released",
            "browser_disposition",
        }
        request_id = frame.get("request_id")
        terminal = frame.get("terminal")
        disposition = frame.get("browser_disposition")
        proof_names = (
            "task_worker_dead",
            "result_gate_closed",
            "cdp_session_detached",
            "collection_lease_released",
        )
        if (
            set(frame) != expected
            or frame.get("schema_version") != SCHEMA_VERSION
            or not isinstance(request_id, str)
            or _REQUEST_ID.fullmatch(request_id) is None
            or terminal not in {"completed", "failed", "cancelled_task_only"}
            or disposition not in {"preserved", "lost"}
            or any(type(frame.get(name)) is not bool for name in proof_names)
            or any(frame[name] is not True for name in proof_names)
        ):
            raise XhsProcessError(
                "xhs_protocol_invalid",
                "task terminal 缺少完整且一致的任务级收据。",
            )
        return cls(
            request_id=request_id,
            terminal=terminal,
            task_worker_dead=True,
            result_gate_closed=True,
            cdp_session_detached=True,
            collection_lease_released=True,
            browser_disposition=disposition,
        )


@dataclass(frozen=True)
class XhsTaskOutcome:
    receipt: XhsTerminalReceipt
    completion: str
    reason_code: str
    items: tuple[CollectedItem, ...]


class _CollectionLease:
    def __init__(self, path: Path, token: str) -> None:
        self.path = path
        self.token = token
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.released = True
            return
        except (OSError, ValueError, TypeError) as exc:
            raise XhsProcessError(
                "xhs_task_cancel_unproven",
                "collection lease 无法验证或释放。",
            ) from exc
        if not isinstance(payload, dict) or payload.get("token") != self.token:
            raise XhsProcessError(
                "xhs_task_cancel_unproven",
                "collection lease 所有权发生变化。",
            )
        try:
            self.path.unlink()
            ProfileRegistry._fsync_directory(self.path.parent)
        except OSError as exc:
            raise XhsProcessError(
                "xhs_task_cancel_unproven",
                "collection lease 无法持久释放。",
            ) from exc
        if self.path.exists():
            raise XhsProcessError(
                "xhs_task_cancel_unproven",
                "collection lease 仍未释放。",
            )
        self.released = True


def _acquire_collection_lease(
    registry: ProfileRegistry,
    request_id: str,
) -> _CollectionLease:
    registry.resolve("xiaohongshu", "default", create=True)
    root = registry.managed_root / ".xhs_browser"
    registry._assert_no_symlink(registry.app_data, root)
    root.mkdir(parents=True, exist_ok=True)
    registry._assert_no_symlink(registry.app_data, root)
    path = root / "collection.lock"
    registry._reject_symlink(path)
    token = uuid4().hex
    payload = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "xhs_collection",
            "request_id": request_id,
            "owner_pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "token": token,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise XhsProcessError(
            "xhs_collection_busy",
            "另一个小红书采集任务仍在运行。",
        ) from exc
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    ProfileRegistry._fsync_directory(root)
    return _CollectionLease(path, token)


def _send(connection: Any, frame: Mapping[str, Any]) -> None:
    raw = json.dumps(
        dict(frame),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise XhsProcessError("xhs_protocol_invalid", "XHS IPC 超出大小上限。")
    connection.send_bytes(raw)


def _receive(connection: Any) -> dict[str, Any]:
    try:
        raw = connection.recv_bytes(MAX_FRAME_BYTES)
        payload = json.loads(raw.decode("utf-8"))
    except (EOFError, OSError, UnicodeError, ValueError) as exc:
        raise XhsProcessError("xhs_protocol_invalid", "XHS IPC 损坏。") from exc
    if not isinstance(payload, dict):
        raise XhsProcessError("xhs_protocol_invalid", "XHS IPC 结构损坏。")
    return payload


def _browser_identity_id(identity: XhsBrowserIdentity) -> str:
    raw = (
        f"{identity.chrome_pid}\0{identity.chrome_birth}\0"
        f"{identity.profile_identity_digest}"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_collect_request(
    homepage_url: str,
    browser_identity: XhsBrowserIdentity,
    *,
    request_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    request_id = request_id or uuid4().hex
    if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
        raise XhsProcessError("xhs_protocol_invalid", "request_id 非法。")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise XhsProcessError("xhs_protocol_invalid", "limit 非法。")
    endpoint = browser_identity.endpoint
    if (
        endpoint.host != "127.0.0.1"
        or isinstance(endpoint.port, bool)
        or not isinstance(endpoint.port, int)
        or not 1 <= endpoint.port <= 65535
        or _BROWSER_PATH.fullmatch(endpoint.browser_path) is None
    ):
        raise XhsProcessError("xhs_protocol_invalid", "P0 浏览器端点身份非法。")
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "operation": "collect_public_homepage",
        "homepage_url": str(homepage_url),
        "limit": limit,
        "browser_identity_id": _browser_identity_id(browser_identity),
        "cdp_host": "127.0.0.1",
        "cdp_port": endpoint.port,
        "browser_ws_path": endpoint.browser_path,
    }


def _default_browser_alive(identity: XhsBrowserIdentity) -> bool:
    try:
        birth = subprocess.check_output(
            ["ps", "-p", str(identity.chrome_pid), "-o", "lstart="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(birth and birth == identity.chrome_birth)


def _item_from_payload(payload: object) -> CollectedItem:
    if not isinstance(payload, dict):
        raise XhsProcessError("xhs_protocol_invalid", "XHS item 结构损坏。")
    try:
        return CollectedItem(**payload)
    except (TypeError, ValueError) as exc:
        raise XhsProcessError("xhs_protocol_invalid", "XHS item 非法。") from exc


def _result_from_frame(frame: Mapping[str, Any]) -> CollectResult:
    expected = {
        "schema_version",
        "request_id",
        "event",
        "completion",
        "reason_code",
        "next_action",
        "confirmed_empty",
        "cursor",
        "items",
    }
    items = frame.get("items")
    cursor = frame.get("cursor")
    if (
        set(frame) != expected
        or frame.get("schema_version") != SCHEMA_VERSION
        or frame.get("event") != "result"
        or not isinstance(items, list)
        or len(items) > 20
        or not isinstance(frame.get("completion"), str)
        or not isinstance(frame.get("reason_code"), str)
        or not isinstance(frame.get("next_action"), str)
        or type(frame.get("confirmed_empty")) is not bool
        or not isinstance(cursor, dict)
    ):
        raise XhsProcessError("xhs_protocol_invalid", "XHS result 收据损坏。")
    normalized = tuple(_item_from_payload(item) for item in items)
    try:
        return CollectResult(
            items=normalized,
            completion=frame["completion"],
            reason_code=frame["reason_code"],
            next_action=frame["next_action"],
            cursor=cursor or None,
            confirmed_empty=frame["confirmed_empty"],
        )
    except ValueError as exc:
        raise XhsProcessError("xhs_protocol_invalid", "XHS result 合同非法。") from exc


class XhsProcessClient:
    """单次 task worker 客户端；不持有任何 Chrome 终止能力。"""

    def __init__(
        self,
        app_data_dir: Path | str,
        browser_identity: XhsBrowserIdentity,
        homepage_url: str,
        *,
        request_id: str | None = None,
        limit: int = 20,
        fixture_mode: str | None = None,
        policy: CleanupPolicy = CleanupPolicy(),
        browser_is_alive: Callable[[XhsBrowserIdentity], bool] = _default_browser_alive,
        process_context: str = "spawn",
    ) -> None:
        self.registry = ProfileRegistry(Path(app_data_dir))
        self.browser_identity = browser_identity
        self.request = build_collect_request(
            homepage_url,
            browser_identity,
            request_id=request_id,
            limit=limit,
        )
        self.request_id = self.request["request_id"]
        self.fixture_mode = fixture_mode
        self.policy = policy
        self.browser_is_alive = browser_is_alive
        self.context = multiprocessing.get_context(process_context)
        self._parent_connection: Any = None
        self._process: Any = None
        self._lease: _CollectionLease | None = None
        self._cancel_started: float | None = None
        self._result_gate_open = True
        self._result: CollectResult | None = None
        self._worker_done: dict[str, Any] | None = None
        self._worker_failed = False
        self._session_attached = False
        self._final: XhsTaskOutcome | None = None

    @property
    def worker_pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def result_gate_open(self) -> bool:
        return self._result_gate_open

    @property
    def collection_lock_path(self) -> Path | None:
        return self._lease.path if self._lease is not None else None

    def start(self) -> None:
        if self._process is not None:
            raise XhsProcessError("xhs_protocol_invalid", "task worker 已启动。")
        try:
            self._lease = _acquire_collection_lease(self.registry, self.request_id)
        except ProfileError as exc:
            raise XhsProcessError(exc.code, str(exc)) from exc
        parent, child = self.context.Pipe(duplex=True)
        process = self.context.Process(
            target=task_worker_main,
            kwargs={
                "connection": child,
                "fixture_mode": self.fixture_mode,
            },
        )
        try:
            process.start()
            child.close()
            self._parent_connection = parent
            self._process = process
            _send(parent, self.request)
        except Exception:
            try:
                parent.close()
                child.close()
            except OSError:
                pass
            if process.is_alive():
                process.terminate()
                process.join(1.0)
            if self._lease is not None:
                self._lease.release()
                self._lease = None
            raise

    def cancel(self) -> None:
        if self._final is not None:
            return
        if self._process is None:
            raise XhsProcessError("xhs_protocol_invalid", "task worker 尚未启动。")
        if self._cancel_started is None:
            self._cancel_started = time.monotonic()
            self._result_gate_open = False
            try:
                _send(self._parent_connection, {
                    "schema_version": SCHEMA_VERSION,
                    "request_id": self.request_id,
                    "control": "cancel",
                })
            except (BrokenPipeError, EOFError, OSError):
                pass

    def _accept_frame(
        self,
        frame: Mapping[str, Any],
        *,
        on_progress: Callable[[str, int], None] | None,
        on_result: Callable[[CollectResult], None] | None,
    ) -> None:
        if (
            frame.get("schema_version") != SCHEMA_VERSION
            or frame.get("request_id") != self.request_id
            or not isinstance(frame.get("event"), str)
        ):
            self._worker_failed = True
            self._result_gate_open = False
            return
        event = frame["event"]
        if event == "progress":
            if (
                self._result_gate_open
                and isinstance(frame.get("label"), str)
                and isinstance(frame.get("count"), int)
                and not isinstance(frame.get("count"), bool)
                and on_progress is not None
            ):
                on_progress(frame["label"], frame["count"])
            return
        if event == "result":
            if not self._result_gate_open:
                return
            try:
                result = _result_from_frame(frame)
            except XhsProcessError:
                self._worker_failed = True
                self._result_gate_open = False
                return
            self._result = result
            if on_result is not None:
                on_result(result)
            return
        if event == "worker_done":
            if (
                set(frame) != {
                    "schema_version",
                    "request_id",
                    "event",
                    "completion",
                    "cdp_session_detached",
                }
                or frame.get("completion")
                not in {"complete", "partial", "cancelled", "failed"}
                or type(frame.get("cdp_session_detached")) is not bool
            ):
                self._worker_failed = True
            else:
                self._worker_done = dict(frame)
            return
        if event == "worker_failed":
            self._worker_failed = True
            return
        if event == "session_attached":
            if set(frame) != {
                "schema_version",
                "request_id",
                "event",
            }:
                self._worker_failed = True
                self._result_gate_open = False
                return
            self._session_attached = True
            return
        self._worker_failed = True
        self._result_gate_open = False

    def _drain(
        self,
        *,
        on_progress: Callable[[str, int], None] | None,
        on_result: Callable[[CollectResult], None] | None,
    ) -> None:
        if self._parent_connection is None:
            return
        while self._parent_connection.poll(0):
            try:
                frame = _receive(self._parent_connection)
            except XhsProcessError:
                if self._worker_done is not None:
                    return
                self._worker_failed = True
                self._result_gate_open = False
                return
            self._accept_frame(
                frame,
                on_progress=on_progress,
                on_result=on_result,
            )

    def wait_until_attached(self, timeout: float) -> bool:
        """离线测试/上层状态同步门：只确认本次 CDP session 已建立。"""
        if self._process is None:
            raise XhsProcessError("xhs_protocol_invalid", "task worker 尚未启动。")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout 必须为正数")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain(on_progress=None, on_result=None)
            if self._session_attached:
                return True
            if not self._process.is_alive():
                self._process.join(0)
                self._drain(on_progress=None, on_result=None)
                return self._session_attached
            time.sleep(0.005)
        return False

    def _force_stop_task_worker(self) -> None:
        if self._process is None or not self._process.is_alive():
            return
        self._process.terminate()
        self._process.join(self.policy.terminate_seconds)
        if self._process.is_alive():
            self._process.kill()
            remaining = max(
                0.05,
                self.policy.total_seconds
                - (time.monotonic() - (self._cancel_started or time.monotonic())),
            )
            self._process.join(remaining)
        if self._process.is_alive():
            raise XhsProcessError(
                "xhs_task_cancel_unproven",
                "task worker 未能在 3 秒内停止。",
            )

    def _finish(self) -> XhsTaskOutcome:
        if self._final is not None:
            return self._final
        if self._process is None or self._process.is_alive():
            raise XhsProcessError(
                "xhs_task_cancel_unproven",
                "task worker 尚未确认退出。",
            )
        self._process.join(0)
        if self._lease is not None:
            self._lease.release()
            self._lease = None
        if self._parent_connection is not None:
            try:
                self._parent_connection.close()
            except OSError:
                pass
        browser_disposition = (
            "preserved"
            if self.browser_is_alive(self.browser_identity)
            else "lost"
        )
        cancelled = self._cancel_started is not None
        result = None if cancelled or self._worker_failed else self._result
        if cancelled:
            terminal = "cancelled_task_only"
            completion = "cancelled"
            reason_code = "cancelled"
            items: tuple[CollectedItem, ...] = ()
        elif (
            result is not None
            and self._worker_done is not None
            and self._worker_done.get("cdp_session_detached") is True
        ):
            terminal = (
                "completed"
                if result.completion in {"complete", "partial"}
                else "failed"
            )
            completion = result.completion
            reason_code = result.reason_code
            items = result.items
        else:
            terminal = "failed"
            completion = "failed"
            reason_code = "network_error"
            items = ()
        self._result_gate_open = False
        receipt = XhsTerminalReceipt.from_frame({
            "schema_version": SCHEMA_VERSION,
            "request_id": self.request_id,
            "terminal": terminal,
            "task_worker_dead": True,
            "result_gate_closed": True,
            "cdp_session_detached": True,
            "collection_lease_released": True,
            "browser_disposition": browser_disposition,
        })
        self._final = XhsTaskOutcome(
            receipt=receipt,
            completion=completion,
            reason_code=reason_code,
            items=items,
        )
        return self._final

    def next_outcome(
        self,
        timeout: float,
        *,
        on_progress: Callable[[str, int], None] | None = None,
        on_result: Callable[[CollectResult], None] | None = None,
    ) -> XhsTaskOutcome:
        if self._final is not None:
            return self._final
        if self._process is None:
            raise XhsProcessError("xhs_protocol_invalid", "task worker 尚未启动。")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout 必须为正数")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain(on_progress=on_progress, on_result=on_result)
            if not self._process.is_alive():
                self._process.join(0.1)
                self._drain(on_progress=on_progress, on_result=on_result)
                return self._finish()
            if (
                self._cancel_started is not None
                and time.monotonic() - self._cancel_started
                >= self.policy.graceful_seconds
            ):
                self._force_stop_task_worker()
                self._drain(on_progress=on_progress, on_result=on_result)
                return self._finish()
            time.sleep(0.005)
        raise TimeoutError("XHS task outcome timeout")

    def next_receipt(self, timeout: float) -> XhsTerminalReceipt:
        return self.next_outcome(timeout).receipt

    def join(self, timeout: float) -> bool:
        if self._process is None:
            return True
        self._process.join(timeout)
        return not self._process.is_alive()

    def close(self) -> None:
        if self._process is None or self._final is not None:
            return
        self.cancel()
        self.next_outcome(self.policy.total_seconds + 0.2)


__all__ = [
    "CleanupPolicy",
    "XhsProcessClient",
    "XhsProcessError",
    "XhsTaskOutcome",
    "XhsTerminalReceipt",
    "build_collect_request",
]
