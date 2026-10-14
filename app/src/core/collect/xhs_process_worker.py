"""小红书单次采集 task worker。

worker 只 attach P0 已验证的回环 CDP，运行一次 adapter 后 detach。本进程从不
拥有、关闭或终止 Chrome；被监督层强制结束时，OS 会同时关闭本进程的 CDP
socket，而专用浏览器继续存在。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

from .adapters import homepage_collection_available
from .contracts import CollectRequest, LinkIdentity


SCHEMA_VERSION = 2
MAX_FRAME_BYTES = 65_536
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_IDENTITY_ID = re.compile(r"[a-f0-9]{64}\Z")
_BROWSER_PATH = re.compile(r"/devtools/browser/[A-Za-z0-9._-]{1,200}\Z")


class WorkerProtocolError(RuntimeError):
    pass


def _send(connection: Any, frame: Mapping[str, Any]) -> None:
    raw = json.dumps(
        dict(frame),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise WorkerProtocolError("xhs_ipc_oversize")
    connection.send_bytes(raw)


def _receive(connection: Any) -> dict[str, Any]:
    raw = connection.recv_bytes(MAX_FRAME_BYTES)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise WorkerProtocolError("xhs_protocol_invalid")
    return payload


def _validate_request(frame: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version",
        "request_id",
        "operation",
        "homepage_url",
        "limit",
        "browser_identity_id",
        "cdp_host",
        "cdp_port",
        "browser_ws_path",
    }
    request_id = frame.get("request_id")
    identity_id = frame.get("browser_identity_id")
    host = frame.get("cdp_host")
    port = frame.get("cdp_port")
    path = frame.get("browser_ws_path")
    homepage = frame.get("homepage_url")
    limit = frame.get("limit")
    try:
        parsed_homepage = urlsplit(str(homepage or ""))
        homepage_port = parsed_homepage.port
    except ValueError as exc:
        raise WorkerProtocolError("xhs_protocol_invalid") from exc
    if (
        set(frame) != expected
        or frame.get("schema_version") != SCHEMA_VERSION
        or frame.get("operation") != "collect_public_homepage"
        or not isinstance(request_id, str)
        or _REQUEST_ID.fullmatch(request_id) is None
        or not isinstance(identity_id, str)
        or _IDENTITY_ID.fullmatch(identity_id) is None
        or host != "127.0.0.1"
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
        or not isinstance(path, str)
        or _BROWSER_PATH.fullmatch(path) is None
        or parsed_homepage.scheme != "https"
        or parsed_homepage.hostname not in {
            "xiaohongshu.com",
            "www.xiaohongshu.com",
        }
        or parsed_homepage.username is not None
        or parsed_homepage.password is not None
        or homepage_port not in {None, 443}
        or parsed_homepage.query
        or parsed_homepage.fragment
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 20
    ):
        raise WorkerProtocolError("xhs_protocol_invalid")
    return dict(frame)


def _receive_control(connection: Any, request_id: str) -> str | None:
    if not connection.poll(0):
        return None
    try:
        frame = _receive(connection)
    except (EOFError, OSError, UnicodeError, ValueError, WorkerProtocolError):
        return "cancel"
    if (
        set(frame) != {"schema_version", "request_id", "control"}
        or frame.get("schema_version") != SCHEMA_VERSION
        or frame.get("request_id") != request_id
        or frame.get("control") != "cancel"
    ):
        return "cancel"
    return "cancel"


class _AttachedPage:
    """DrissionPage 的 attach-only 包装；detach 不触发 Browser.close。"""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._detached = False

    def get(self, url: str) -> Any:
        return self._page.get(url)

    def run_js(self, script: str) -> Any:
        return self._page.run_js(script)

    def detach(self) -> None:
        if self._detached:
            return
        self._detached = True
        self._page.disconnect()


def _attach_verified_session(endpoint: Mapping[str, Any]) -> _AttachedPage:
    """只 attach P0 交付的 127.0.0.1 动态端口，不探测或猜测其他浏览器。"""
    from DrissionPage import ChromiumOptions, ChromiumPage

    options = ChromiumOptions(read_file=False)
    options.set_address(f"127.0.0.1:{endpoint['cdp_port']}")
    options.existing_only(True)
    return _AttachedPage(ChromiumPage(options))


def _fixture_row() -> dict[str, object]:
    return {
        "work_id": "fixture_work",
        "work_url": "https://www.xiaohongshu.com/explore/fixture_work",
        "author_id": "fixture_author",
        "author_name": "fixture",
        "title": "fixture",
        "content_kind": "unknown",
        "cover_url": "",
    }


class _FixtureSession:
    def __init__(
        self,
        *,
        cancelled: Callable[[], bool],
        block_navigation: bool = False,
    ) -> None:
        self.cancelled = cancelled
        self.block_navigation = block_navigation
        self.detached = False
        self._read = False

    def get(self, _url: str) -> None:
        if not self.block_navigation:
            return
        while not self.cancelled():
            time.sleep(0.01)

    def run_js(self, script: str) -> object:
        if "scrollBy" in script:
            return None
        if self._read:
            return {"status": "ok", "items": [], "has_more": False}
        self._read = True
        return {"status": "ok", "items": [_fixture_row()], "has_more": False}

    def detach(self) -> None:
        self.detached = True


def _result_frame(request_id: str, result: Any) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "event": "result",
        "completion": result.completion,
        "reason_code": result.reason_code,
        "next_action": result.next_action,
        "confirmed_empty": result.confirmed_empty,
        "cursor": dict(result.cursor or {}),
        "items": [item.to_dict() for item in result.items],
    }


def task_worker_main(
    connection: Any,
    *,
    fixture_mode: str | None = None,
) -> None:
    """运行一次 attach/collect/detach；异常只返回固定错误码。"""
    request_id = "unknown"
    session: Any = None
    detached = False
    cancelled = False
    try:
        if not homepage_collection_available("xiaohongshu"):
            _send(connection, {
                "schema_version": SCHEMA_VERSION,
                "request_id": request_id,
                "event": "worker_failed",
                "code": "release_disabled",
                "cdp_session_detached": False,
            })
            return
        from .adapters.xiaohongshu import XiaohongshuAdapter

        request = _validate_request(_receive(connection))
        request_id = request["request_id"]

        def is_cancelled() -> bool:
            nonlocal cancelled
            if cancelled:
                return True
            cancelled = _receive_control(connection, request_id) == "cancel"
            return cancelled

        if fixture_mode == "ignore_cancel":
            _send(connection, {
                "schema_version": SCHEMA_VERSION,
                "request_id": request_id,
                "event": "session_attached",
            })
            while True:
                time.sleep(1)

        if fixture_mode == "late_result_after_cancel":
            _send(connection, {
                "schema_version": SCHEMA_VERSION,
                "request_id": request_id,
                "event": "session_attached",
            })
            while not is_cancelled():
                time.sleep(0.01)
            _send(connection, {
                "schema_version": SCHEMA_VERSION,
                "request_id": request_id,
                "event": "progress",
                "label": "late progress must be discarded",
                "count": 1,
            })
            fake = _FixtureSession(cancelled=is_cancelled)
            adapter = XiaohongshuAdapter(open_session_fn=lambda _profile: fake)
            result = adapter.collect(
                CollectRequest(
                    LinkIdentity(
                        platform="xiaohongshu",
                        kind="homepage",
                        canonical_url=request["homepage_url"],
                        extracted_id="fixture",
                        confidence="exact",
                    ),
                    mode="profile_public",
                    limit=request["limit"],
                    profile_id="default",
                ),
                is_cancelled=lambda: False,
            )
            _send(connection, _result_frame(request_id, result))
            while True:
                time.sleep(1)

        if fixture_mode is not None:
            session = _FixtureSession(
                cancelled=is_cancelled,
                block_navigation=fixture_mode == "cooperative_cancel",
            )
        else:
            session = _attach_verified_session(request)
        _send(connection, {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "event": "session_attached",
        })

        adapter = XiaohongshuAdapter(open_session_fn=lambda _profile: session)
        collect_request = CollectRequest(
            LinkIdentity(
                platform="xiaohongshu",
                kind="homepage",
                canonical_url=request["homepage_url"],
                extracted_id="profile",
                confidence="exact",
            ),
            mode="profile_public",
            limit=request["limit"],
            profile_id="default",
        )
        result = adapter.collect(
            collect_request,
            progress=lambda label, count: _send(connection, {
                "schema_version": SCHEMA_VERSION,
                "request_id": request_id,
                "event": "progress",
                "label": str(label)[:160],
                "count": int(count),
            }),
            is_cancelled=is_cancelled,
        )
        detached = bool(getattr(session, "detached", True))
        _send(connection, _result_frame(request_id, result))
        _send(connection, {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "event": "worker_done",
            "completion": result.completion,
            "cdp_session_detached": detached,
        })
    except BaseException:  # noqa: BLE001 - 子进程只输出固定错误码
        try:
            if session is not None and not detached:
                session.detach()
                detached = True
            _send(connection, {
                "schema_version": SCHEMA_VERSION,
                "request_id": request_id,
                "event": "worker_failed",
                "code": "xhs_task_worker_failed",
                "cdp_session_detached": detached,
            })
        except Exception:
            pass
    finally:
        try:
            connection.close()
        except Exception:
            pass


__all__ = [
    "MAX_FRAME_BYTES",
    "SCHEMA_VERSION",
    "WorkerProtocolError",
    "task_worker_main",
]
