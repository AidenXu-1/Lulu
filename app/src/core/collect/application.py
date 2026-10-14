"""主页采集应用服务。

UI 只消费本模块的 DTO，不直接接触平台 adapter、browser shell 或 profile。
链接识别保持纯本地；浏览器会话只在用户显式采集或打开登录窗口时惰性创建。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import RLock
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

from .adapters import (
    AdapterUnavailableError,
    release_adapter_capability,
    resolve_adapter,
)
from .browser_shell import BrowserLaunchSpec, BrowserShell
from .contracts import AdapterCapability, CollectRequest, CollectResult, LinkIdentity
from .cover_workflow import CoverWorkflowResult
from .models import CollectedItem
from .preferences import CollectCoverPreferenceSnapshot
from .profiles import ProfileRegistry
from .router import detect_link

ProgressCallback = Callable[[str, int], None]
CancelCallback = Callable[[], bool]
SessionFactory = Callable[[str, str, str | None, CancelCallback], Any]
Sleeper = Callable[[float], None]

DOUYIN_MODE_EFFICIENCY = "efficiency"
DOUYIN_MODE_LOW_RISK = "low_risk"
_XHS_LATE_READINESS_SECONDS = 30.0
XHS_RELEASE_DISABLED_MESSAGE = (
    "小红书主页采集本版暂缓开放，不会启动浏览器。"
    "单条作品仍可到“文案提取”使用。"
)


class CoverWorkflowRunner(Protocol):
    def run(
        self,
        result: CollectResult,
        *,
        snapshot: CollectCoverPreferenceSnapshot,
        is_cancelled: CancelCallback | None = None,
    ) -> CoverWorkflowResult: ...


class _OwnedSession:
    """让 adapter 与 service 的双重 finally 只关闭底层会话一次。"""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._session.close()


@dataclass(frozen=True)
class CollectionEntryState:
    """输入框的无副作用识别结果。"""

    identity: LinkIdentity
    capability: AdapterCapability
    allowed_modes: tuple[str, ...]


@dataclass(frozen=True)
class CollectionCommand:
    """一次采集启动时冻结的页面输入。"""

    raw_input: str
    mode: str
    limit: int | None = None
    include_cover: bool = False
    profile_id: str = "default"
    browser_path: str | None = None
    browser_mode: str | None = None


@dataclass(frozen=True)
class CollectionCoverSummary:
    """一次主页采集的封面子结果，不改变主采集 completion。"""

    enabled: bool = False
    eligible_count: int = 0
    downloaded_count: int = 0
    failed_count: int = 0
    cancelled_count: int = 0
    recovered_count: int = 0
    code: str = "not_requested"


@dataclass(frozen=True)
class CollectionExecutionResult:
    """UI worker 的稳定返回：主采集结果与封面汇总分开携带。"""

    collect_result: CollectResult
    cover_summary: CollectionCoverSummary = CollectionCoverSummary()


@dataclass(frozen=True)
class XhsBrowserStatus:
    """UI 可消费的脱敏浏览器状态；不暴露 PID、Profile 或 CDP endpoint。"""

    code: str = "absent"
    running: bool = False
    busy: bool = False
    message: str = "小红书专用浏览器尚未打开。"


_XHS_STATUS_MESSAGES = {
    "absent": "小红书专用浏览器尚未打开。",
    "opening": "正在打开小红书专用浏览器…",
    "ready_idle": "专用浏览器已打开；登录状态由平台页面确认。",
    "collecting": "专用浏览器已打开，正在执行本次采集。",
    "detaching_task": "正在停止本次采集；专用浏览器保持打开。",
    "collection_busy": "正在采集中，请先停止本次采集。",
    "task_cleanup_unproven": (
        "本次采集的停止状态尚未确认；专用浏览器保持原状，请退出 Lulu 后重试。"
    ),
    "retained": "专用浏览器保持打开，Lulu 已释放本次控制权。",
    "browser_lost": "专用浏览器已关闭；重试会重新打开。",
    "close_failed_browser_alive": (
        "专用浏览器仍在运行。请在浏览器窗口中正常退出，或稍后重试。"
    ),
    "chrome_missing": "未发现 Google Chrome，请先自行安装后重试。",
    "chrome_version_unsupported": "请将 Google Chrome 更新到 136 或更高版本。",
    "profile_identity_blocked": "专用浏览器档案身份无法安全验证，已停止。",
    "profile_in_use_unknown": "专用浏览器档案正被未知进程占用，请先处理。",
    "controller_busy": "另一个 Lulu 正在使用小红书专用浏览器。",
    "cdp_discovery_invalid": "专用浏览器连接尚未可信就绪，请重试。",
    "cdp_exposure_blocked": "专用浏览器连接身份不安全，已停止。",
    "login_storage_unavailable": "Chrome 无法复用安全登录存储，请重新打开或查看说明。",
    "release_disabled": XHS_RELEASE_DISABLED_MESSAGE,
}


def _xhs_status(code: str, *, running: bool = False, busy: bool = False) -> XhsBrowserStatus:
    return XhsBrowserStatus(
        code=code,
        running=running,
        busy=busy,
        message=_XHS_STATUS_MESSAGES.get(
            code,
            "小红书专用浏览器未能继续，本次已安全停止。",
        ),
    )


def _request_xhs_normal_close(identity: Any) -> bool:
    """通过已验证的动态回环端点请求 Chrome 正常退出；绝不发送系统信号。"""
    try:
        from DrissionPage import ChromiumOptions, ChromiumPage

        options = ChromiumOptions(read_file=False)
        options.set_address(f"127.0.0.1:{identity.endpoint.port}")
        options.existing_only(True)
        page = ChromiumPage(options)
        page.quit(timeout=3.0)
        return True
    except Exception:  # noqa: BLE001 - 对 UI 只返回稳定的关闭失败状态
        return False


def _xhs_browser_manager_type() -> type:
    from .xhs_browser import XhsBrowserManager

    return XhsBrowserManager


def _xhs_process_factory_type() -> type:
    from .xhs_process import XhsProcessClient

    return XhsProcessClient


def _default_xhs_browser_manager() -> Any:
    from ..models_manager import app_data_dir

    return _xhs_browser_manager_type()(
        ProfileRegistry(app_data_dir()),
        normal_close=_request_xhs_normal_close,
        discovery_timeout=_XHS_LATE_READINESS_SECONDS,
    )


def _default_session_factory(
    platform: str,
    profile_id: str,
    browser_path: str | None,
    is_cancelled: CancelCallback,
) -> Any:
    from ..models_manager import app_data_dir

    shell = BrowserShell(ProfileRegistry(app_data_dir()))
    return shell.open(
        BrowserLaunchSpec(platform, profile_id=profile_id, browser_path=browser_path),
        is_cancelled=is_cancelled,
    )


def _input_failure(identity: LinkIdentity) -> CollectResult:
    reason = identity.reason_code or "unsupported_link"
    if reason == "ambiguous_link":
        action = "一次只粘贴一个主页链接。"
    elif reason == "invalid_input":
        action = "请粘贴抖音或小红书公开主页链接。"
    else:
        action = "当前不支持这个链接，请粘贴公开主页链接。"
    return CollectResult.failed(reason, next_action=action)


class CollectionApplicationService:
    """把 router、能力门、adapter 与受控浏览器会话组合成 UI 边界。"""

    def __init__(
        self,
        *,
        adapter_resolver: Callable[[str], Any] = resolve_adapter,
        session_factory: SessionFactory = _default_session_factory,
        sleeper: Sleeper = time.sleep,
        cover_workflow: CoverWorkflowRunner | None = None,
        xhs_browser_manager: Any | None = None,
        xhs_process_factory: Callable[..., Any] | None = None,
        capability_resolver: Callable[[str], AdapterCapability] = release_adapter_capability,
        xhs_poll_seconds: float = 0.05,
    ) -> None:
        self._adapter_resolver = adapter_resolver
        self._session_factory = session_factory
        self._sleeper = sleeper
        self._cover_workflow = cover_workflow
        self._session_lock = RLock()
        self._active_session: Any | None = None
        if (
            isinstance(xhs_poll_seconds, bool)
            or not isinstance(xhs_poll_seconds, (int, float))
            or not 0.01 <= xhs_poll_seconds <= 1.0
        ):
            raise ValueError("xhs_poll_seconds 必须在 0.01 到 1.0 秒之间")
        self._capability_resolver = capability_resolver
        self._xhs_manager = xhs_browser_manager
        self._xhs_process_factory = xhs_process_factory
        self._xhs_poll_seconds = float(xhs_poll_seconds)
        self._xhs_lock = RLock()
        self._xhs_identity: Any | None = None
        self._xhs_client: Any | None = None
        self._xhs_status = _xhs_status("absent")

    def inspect(self, raw_input: str) -> CollectionEntryState:
        identity = detect_link(raw_input)
        capability = self._capability_resolver(identity.platform)
        return CollectionEntryState(identity, capability, capability.modes)

    def _xhs_release_enabled(self) -> bool:
        return self._capability_resolver("xiaohongshu").available

    def _ensure_xhs_manager(self) -> Any:
        if not self._xhs_release_enabled():
            raise RuntimeError("release_disabled")
        if self._xhs_manager is None:
            self._xhs_manager = _default_xhs_browser_manager()
        return self._xhs_manager

    def _ensure_xhs_process_factory(self) -> Callable[..., Any]:
        if not self._xhs_release_enabled():
            raise RuntimeError("release_disabled")
        if self._xhs_process_factory is None:
            self._xhs_process_factory = _xhs_process_factory_type()
        return self._xhs_process_factory

    def collect(
        self,
        command: CollectionCommand,
        *,
        progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> CollectResult:
        cancelled = is_cancelled or (lambda: False)
        if cancelled():
            return CollectResult.cancelled()

        entry = self.inspect(command.raw_input)
        identity = entry.identity
        if identity.platform == "unknown":
            return _input_failure(identity)
        if (
            identity.platform == "xiaohongshu"
            and entry.capability.reason == "release_disabled"
        ):
            return CollectResult.failed(
                "unsupported_link",
                next_action=XHS_RELEASE_DISABLED_MESSAGE,
            )
        if identity.platform == "bilibili" or not entry.capability.available:
            return CollectResult.failed(
                "unsupported_link",
                next_action="B站主页采集暂不可用，现有单链接能力不受影响。",
            )
        if identity.platform == "xiaohongshu" and identity.kind != "homepage":
            return CollectResult.failed(
                "unsupported_link",
                next_action="小红书当前只支持公开主页采集。",
            )
        if command.mode not in entry.allowed_modes:
            return CollectResult.failed(
                "unsupported_link",
                next_action="当前平台不支持所选采集方式。",
            )
        if identity.platform == "xiaohongshu" and command.profile_id != "default":
            return CollectResult.failed(
                "invalid_input",
                next_action="小红书当前只使用 Lulu 专用 default profile。",
            )

        request = CollectRequest(
            identity=identity,
            mode=command.mode,
            limit=command.limit,
            include_cover=command.include_cover,
            include_media=False,
            profile_id=command.profile_id,
        )
        if identity.platform == "xiaohongshu":
            return self._collect_xhs(
                request,
                progress=progress,
                is_cancelled=cancelled,
            )

        try:
            adapter = self._adapter_resolver(identity.platform)
        except AdapterUnavailableError:
            return CollectResult.failed(
                "unsupported_link",
                next_action="该平台主页采集暂不可用。",
            )

        try:
            result = adapter.collect(
                request,
                progress=progress,
                is_cancelled=cancelled,
                browser_path=command.browser_path,
                browser_mode=command.browser_mode,
            )
        except Exception:  # noqa: BLE001 - 跨层只返回脱敏稳定错误
            result = CollectResult.failed(
                "network_error",
                next_action="采集过程发生异常，已安全停止，请稍后重试。",
            )

        if cancelled() and result.completion not in {"partial", "cancelled"}:
            return CollectResult.cancelled(result.items)
        return result

    def _collect_xhs(
        self,
        request: CollectRequest,
        *,
        progress: ProgressCallback | None,
        is_cancelled: CancelCallback,
    ) -> CollectResult:
        if not self._xhs_release_enabled():
            return CollectResult.failed(
                "unsupported_link",
                next_action=XHS_RELEASE_DISABLED_MESSAGE,
            )
        status = self.open_xhs_browser()
        if not status.running or self._xhs_identity is None:
            return CollectResult.failed(
                "network_error",
                next_action=status.message,
            )
        with self._xhs_lock:
            if self._xhs_client is not None:
                return CollectResult.failed(
                    "network_error",
                    next_action="已有小红书采集任务正在运行，请先停止。",
                )
            identity = self._xhs_identity
        try:
            manager = self._ensure_xhs_manager()
            process_factory = self._ensure_xhs_process_factory()
            client = process_factory(
                manager.registry.app_data,
                identity,
                request.identity.canonical_url,
                limit=request.limit or 20,
            )
        except Exception:
            return CollectResult.failed(
                "network_error",
                next_action="小红书采集任务未能启动；专用浏览器保持打开，请稍后重试。",
            )
        with self._xhs_lock:
            if self._xhs_client is not None:
                return CollectResult.failed(
                    "network_error",
                    next_action="已有小红书采集任务正在运行，请先停止。",
                )
            self._xhs_client = client
            self._xhs_status = _xhs_status(
                "collecting",
                running=True,
                busy=True,
            )
        cancel_sent = False
        try:
            client.start()
            while True:
                if is_cancelled() and not cancel_sent:
                    cancel_sent = True
                    with self._xhs_lock:
                        self._xhs_status = _xhs_status(
                            "detaching_task",
                            running=True,
                            busy=True,
                        )
                    client.cancel()
                try:
                    outcome = client.next_outcome(
                        self._xhs_poll_seconds,
                        on_progress=progress,
                    )
                    break
                except TimeoutError:
                    continue
        except Exception:
            cleanup_proven = False
            try:
                client.close()
                cleanup_proven = True
            except Exception:
                pass
            browser_alive = self._xhs_browser_alive(identity)
            with self._xhs_lock:
                if cleanup_proven and self._xhs_client is client:
                    self._xhs_client = None
                self._xhs_status = _xhs_status(
                    (
                        "task_cleanup_unproven"
                        if not cleanup_proven
                        else "ready_idle"
                        if browser_alive
                        else "browser_lost"
                    ),
                    running=browser_alive,
                    busy=not cleanup_proven,
                )
            return CollectResult.failed(
                "network_error",
                next_action=self._xhs_status.message,
            )
        with self._xhs_lock:
            if self._xhs_client is client:
                self._xhs_client = None
            self._xhs_status = _xhs_status(
                "ready_idle"
                if outcome.receipt.browser_disposition == "preserved"
                else "browser_lost",
                running=outcome.receipt.browser_disposition == "preserved",
            )
        return self._collect_result_from_xhs_outcome(outcome)

    @staticmethod
    def _collect_result_from_xhs_outcome(outcome: Any) -> CollectResult:
        items = tuple(outcome.items)
        reason = (
            outcome.reason_code
            if outcome.reason_code in {
                "login_required",
                "login_expired",
                "captcha",
                "rate_limited",
                "permission_denied",
                "platform_changed",
                "network_error",
                "no_items_unverified",
                "cancelled",
            }
            else "network_error"
        )
        next_actions = {
            "login_required": "请在小红书专用浏览器中登录后重试。",
            "login_expired": "请在小红书专用浏览器中重新登录后重试。",
            "captcha": "请在小红书专用浏览器中完成验证后重试。",
            "rate_limited": "请稍后再试。",
            "permission_denied": "请确认该主页内容为公开可见。",
            "platform_changed": "请更新 Lulu 后重试。",
            "network_error": "请重新打开专用浏览器后重试。",
            "no_items_unverified": "请确认主页公开可见后重试。",
        }
        if outcome.completion == "complete":
            return CollectResult.complete(
                items,
                confirmed_empty=not items,
            )
        if outcome.completion == "partial" and items:
            return CollectResult.partial(
                items,
                reason_code=reason,
                next_action=next_actions.get(reason, "请稍后重试。"),
            )
        if outcome.completion == "cancelled":
            return CollectResult.cancelled(items)
        return CollectResult.failed(
            reason,
            next_action=next_actions.get(reason, "请稍后重试。"),
        )

    def collect_with_cover(
        self,
        command: CollectionCommand,
        *,
        progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> CollectionExecutionResult:
        """先完成主采集，再对合法作品运行可注入的封面子阶段。"""

        cancelled = is_cancelled or (lambda: False)
        result = self.collect(
            command,
            progress=progress,
            is_cancelled=cancelled,
        )
        workflow = self._cover_workflow
        if not command.include_cover:
            return CollectionExecutionResult(result)
        if workflow is None:
            return CollectionExecutionResult(
                result,
                CollectionCoverSummary(enabled=True, code="entry_disabled"),
            )

        eligible = tuple(item for item in result.items if _cover_eligible(item))
        if not eligible:
            return CollectionExecutionResult(
                result,
                CollectionCoverSummary(enabled=True, code="no_eligible_items"),
            )

        cover_input = CollectResult.complete(eligible)
        try:
            cover_result = workflow.run(
                cover_input,
                snapshot=CollectCoverPreferenceSnapshot(True),
                is_cancelled=cancelled,
            )
            final_items = _merge_cover_items(result.items, cover_result)
            final_result = replace(result, items=final_items)
            summary = _cover_summary(cover_result)
        except Exception:  # noqa: BLE001 - 封面子阶段失败不得污染主结果
            final_items = _mark_cover_workflow_failed(result.items, eligible)
            final_result = replace(result, items=final_items)
            summary = CollectionCoverSummary(
                enabled=True,
                eligible_count=len(eligible),
                failed_count=len(eligible),
                code="workflow_failed",
            )
        return CollectionExecutionResult(final_result, summary)

    def xhs_browser_status(self, *, refresh: bool = False) -> XhsBrowserStatus:
        """返回脱敏状态；refresh 只核对 P0 的运行收据和进程身份。"""
        if not self._xhs_release_enabled():
            return _xhs_status("release_disabled")
        with self._xhs_lock:
            current = self._xhs_status
            identity = self._xhs_identity
            client = self._xhs_client
        if not refresh or client is not None:
            return current
        manager = self._ensure_xhs_manager()
        if identity is not None:
            try:
                alive = (
                    manager.process_probe(
                        identity.chrome_pid,
                        identity.chrome_birth,
                    )
                    is not None
                )
            except Exception:
                alive = False
            status = (
                _xhs_status("ready_idle", running=True)
                if alive
                else _xhs_status("browser_lost")
            )
        elif current.running:
            status = current
        else:
            try:
                receipt = manager.registry.load_xhs_browser_receipt()
                alive = bool(
                    receipt is not None
                    and manager.process_probe(
                        receipt.chrome_pid,
                        receipt.chrome_birth,
                    )
                    is not None
                )
            except Exception:
                alive = False
            status = (
                _xhs_status("cdp_discovery_invalid")
                if alive
                else _xhs_status("absent")
            )
        with self._xhs_lock:
            self._xhs_status = status
        return status

    def _xhs_browser_alive(self, identity: Any | None) -> bool:
        if identity is None or not self._xhs_release_enabled():
            return False
        try:
            return (
                self._ensure_xhs_manager().process_probe(
                    identity.chrome_pid,
                    identity.chrome_birth,
                )
                is not None
            )
        except Exception:
            return False

    def open_xhs_browser(self) -> XhsBrowserStatus:
        """启动或严格接管专用 Chrome；不读取登录数据。"""
        if not self._xhs_release_enabled():
            return _xhs_status("release_disabled")
        with self._xhs_lock:
            if self._xhs_client is not None:
                return _xhs_status("collection_busy", running=True, busy=True)
            self._xhs_status = _xhs_status("opening", busy=True)
        try:
            identity = self._ensure_xhs_manager().ensure_ready()
        except Exception as exc:  # noqa: BLE001 - 跨层只暴露稳定状态
            code = getattr(exc, "code", "browser_lost")
            status = _xhs_status(code)
            with self._xhs_lock:
                self._xhs_identity = None
                self._xhs_status = status
            return status
        status = _xhs_status("ready_idle", running=True)
        with self._xhs_lock:
            self._xhs_identity = identity
            self._xhs_status = status
        return status

    def close_xhs_browser(self) -> XhsBrowserStatus:
        """只响应用户显式出口；活动采集期间拒绝，失败时保持诚实运行态。"""
        if not self._xhs_release_enabled():
            return _xhs_status("release_disabled")
        with self._xhs_lock:
            if self._xhs_client is not None:
                return _xhs_status("collection_busy", running=True, busy=True)
            identity = self._xhs_identity
            current = self._xhs_status
        if identity is None:
            if not current.running:
                return _xhs_status("absent")
            adopted = self.open_xhs_browser()
            if not adopted.running:
                if adopted.code == "browser_lost":
                    return _xhs_status("absent")
                return adopted
        try:
            self._ensure_xhs_manager().close_explicit()
        except Exception as exc:  # noqa: BLE001 - 跨层只暴露稳定状态
            code = getattr(exc, "code", "close_failed_browser_alive")
            running = code == "close_failed_browser_alive"
            status = _xhs_status(
                code,
                running=running,
            )
            with self._xhs_lock:
                self._xhs_status = status
            return status
        status = _xhs_status("absent")
        with self._xhs_lock:
            self._xhs_identity = None
            self._xhs_status = status
        return status

    def release_xhs_browser_control(self) -> XhsBrowserStatus:
        """退出 Lulu 时释放 controller；专用浏览器与 Profile 保持。"""
        if not self._xhs_release_enabled():
            return _xhs_status("release_disabled")
        with self._xhs_lock:
            running = self._xhs_status.running
        if self._xhs_manager is not None:
            self._xhs_manager.release_controller()
        status = (
            _xhs_status("retained", running=True)
            if running
            else _xhs_status("absent")
        )
        with self._xhs_lock:
            self._xhs_identity = None
            self._xhs_status = status
        return status

    def xhs_task_active(self) -> bool:
        if not self._xhs_release_enabled():
            return False
        with self._xhs_lock:
            return self._xhs_client is not None

    def run_profile_login(
        self,
        platform: str,
        *,
        browser_path: str | None = None,
        progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> str:
        """打开常驻专用窗口；登录由用户完成，状态仍由平台采集结果确认。"""
        key = str(platform or "").strip().lower()
        cancelled = is_cancelled or (lambda: False)
        progress = progress or (lambda *_: None)
        if key != "xiaohongshu":
            return "unsupported"
        if not self._xhs_release_enabled():
            return "release_disabled"
        if cancelled():
            return "cancelled"

        progress("正在打开 Lulu 专用小红书浏览器…", 0)
        status = self.open_xhs_browser()
        if cancelled():
            return "cancelled"
        if status.running:
            progress(
                "专用浏览器已打开。请在窗口中完成登录；Lulu 不读取登录内容。",
                0,
            )
            return "ready"
        return status.code

    def close_active_session(self) -> bool:
        """限定关闭本服务当前持有的专用会话；BrowserSession.close() 幂等。"""
        with self._session_lock:
            session = self._active_session
            self._active_session = None
        if session is None:
            return False
        try:
            session.close()
        except Exception:  # noqa: BLE001 - lease 已由 BrowserSession finally 释放
            return False
        return True

    def _open_session(
        self,
        platform: str,
        profile_id: str,
        browser_path: str | None,
        is_cancelled: CancelCallback,
    ) -> Any:
        raw_session = self._session_factory(platform, profile_id, browser_path, is_cancelled)
        if raw_session is None:
            return None
        session = _OwnedSession(raw_session)
        with self._session_lock:
            if self._active_session is not None:
                try:
                    session.close()
                finally:
                    raise RuntimeError("已有采集浏览器会话正在运行")
            self._active_session = session
        return session


def _cover_eligible(item: CollectedItem) -> bool:
    if not item.stable_key or not item.cover_url:
        return False
    try:
        parsed = urlsplit(item.cover_url)
    except ValueError:
        return False
    return bool(
        parsed.scheme.casefold() == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


def _merge_cover_items(
    original: tuple[CollectedItem, ...],
    workflow_result: CoverWorkflowResult,
) -> tuple[CollectedItem, ...]:
    updated = tuple(workflow_result.collect_result.items)
    outcomes = tuple(workflow_result.outcomes)
    if (
        len(updated) != len(outcomes)
        or any(not item.stable_key for item in updated)
        or len({item.stable_key for item in updated}) != len(updated)
        or tuple(item.stable_key for item in updated)
        != tuple(outcome.stable_key for outcome in outcomes)
    ):
        raise ValueError("封面工作流返回的作品身份不完整")
    by_key = {item.stable_key: item for item in updated}
    expected = {
        item.stable_key for item in original if _cover_eligible(item)
    }
    if set(by_key) != expected:
        raise ValueError("封面工作流返回的作品集合漂移")
    return tuple(by_key.get(item.stable_key, item) for item in original)


def _cover_summary(result: CoverWorkflowResult) -> CollectionCoverSummary:
    outcomes = tuple(result.outcomes)
    allowed = {"downloaded", "recovered_previous", "failed", "url_only"}
    if any(outcome.status not in allowed for outcome in outcomes):
        raise ValueError("封面工作流返回了未知状态")
    if any(
        outcome.status == "url_only"
        and outcome.reason_code != "cover_cancelled"
        for outcome in outcomes
    ):
        raise ValueError("合法封面条目不能静默退回 url_only")
    downloaded = sum(
        outcome.status in {"downloaded", "recovered_previous"}
        for outcome in outcomes
    )
    recovered = sum(
        outcome.status == "recovered_previous" for outcome in outcomes
    )
    failed = sum(outcome.status == "failed" for outcome in outcomes)
    cancelled = sum(
        outcome.status == "url_only"
        and outcome.reason_code == "cover_cancelled"
        for outcome in outcomes
    )
    if downloaded + failed + cancelled != len(outcomes):
        raise ValueError("封面工作流汇总不闭合")
    return CollectionCoverSummary(
        enabled=True,
        eligible_count=len(outcomes),
        downloaded_count=downloaded,
        failed_count=failed,
        cancelled_count=cancelled,
        recovered_count=recovered,
        code="settled",
    )


def _mark_cover_workflow_failed(
    original: tuple[CollectedItem, ...],
    eligible: tuple[CollectedItem, ...],
) -> tuple[CollectedItem, ...]:
    keys = {item.stable_key for item in eligible}
    output = []
    for item in original:
        if item.stable_key not in keys:
            output.append(item)
            continue
        error = item.last_error_code
        if not error or error.startswith("cover:"):
            error = "cover:cover_commit_failed"
        output.append(
            replace(
                item,
                cover_local_path="",
                cover_status="failed",
                last_error_code=error,
            )
        )
    return tuple(output)


__all__ = [
    "CollectionCoverSummary",
    "CollectionExecutionResult",
    "CollectionApplicationService",
    "CollectionCommand",
    "CollectionEntryState",
    "DOUYIN_MODE_EFFICIENCY",
    "DOUYIN_MODE_LOW_RISK",
]
