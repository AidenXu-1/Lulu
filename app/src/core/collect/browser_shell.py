"""采集平台共享的浏览器启动壳与物理隔离生命周期。

本模块只管理 Lulu 自有浏览器实例。每个平台使用独立 user-data、Cookie、缓存、
临时目录、profile 锁和调试端口锁；不会扫描或导入日常浏览器身份。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import socket
import sys
from typing import Any

from .profiles import (
    BrowserProfilePaths,
    DebugPortLease,
    ProfileError,
    ProfileLease,
    ProfileRegistry,
)

_XHS_PAGE_LOAD_TIMEOUT_SECONDS = 2.0
_XHS_SCRIPT_TIMEOUT_SECONDS = 2.0
_XHS_CLOSE_TIMEOUT_SECONDS = 0.5


class BrowserShellError(RuntimeError):
    """共享浏览器壳的稳定失败合同。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class BrowserLaunchSpec:
    platform_id: str
    profile_id: str = "default"
    browser_path: str | None = None


def debug_port_for(platform_id: str, profile_id: str = "default") -> int:
    """为平台身份稳定派生互不重叠的本地调试端口段。"""
    ranges = {
        "douyin": (43100, 900),
        "xiaohongshu": (44100, 900),
    }
    platform = str(platform_id or "").strip().lower()
    if platform not in ranges:
        raise BrowserShellError("profile_unavailable", "该平台当前没有浏览器身份能力。")
    profile = str(profile_id or "").strip()
    digest = hashlib.sha256(profile.encode("utf-8")).digest()
    start, size = ranges[platform]
    return start + int.from_bytes(digest[:4], "big") % size


def _port_is_available(port: int) -> bool:
    """只检查本机 loopback 绑定，不连接任何外部地址。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _default_options_factory() -> Any:
    try:
        from DrissionPage import ChromiumOptions
    except Exception as exc:  # noqa: BLE001 - 依赖缺失需稳定翻译
        raise BrowserShellError(
            "dependency_unavailable",
            "需要浏览器驱动组件(DrissionPage)未就绪。",
        ) from exc
    return ChromiumOptions(read_file=False)


def _default_page_factory(options: Any) -> Any:
    try:
        from DrissionPage import ChromiumPage
    except Exception as exc:  # noqa: BLE001 - 依赖缺失需稳定翻译
        raise BrowserShellError(
            "dependency_unavailable",
            "需要浏览器驱动组件(DrissionPage)未就绪。",
        ) from exc
    if sys.platform == "darwin":
        from .macos_browser import create_page
        return create_page(options, ChromiumPage)
    return ChromiumPage(options)


class BrowserSession:
    """代理真实 page，并确保关闭异常时仍释放本会话两把锁。"""

    def __init__(
        self,
        page: Any,
        paths: BrowserProfilePaths,
        debug_port: int,
        profile_lease: ProfileLease,
        port_lease: DebugPortLease,
        close_timeout: float | None = None,
    ) -> None:
        self.page = page
        self.paths = paths
        self.debug_port = debug_port
        self.profile_lease = profile_lease
        self.port_lease = port_lease
        self.close_timeout = close_timeout
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.page, name)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_error: Exception | None = None
        try:
            if self.close_timeout is None:
                self.page.quit()
            else:
                self.page.quit(timeout=self.close_timeout)
        except Exception as exc:  # noqa: BLE001 - 释放锁后再稳定翻译
            close_error = exc
        finally:
            self.port_lease.release()
            self.profile_lease.release()
        if close_error is not None:
            raise BrowserShellError("browser_close_failed", f"浏览器关闭失败:{close_error}") from close_error

    def quit(self) -> None:
        self.close()

    def detach(self) -> None:
        """End this bounded legacy shell session and release both leases.

        Persistent XHS controllers provide a true CDP detach implementation;
        this older shell owns the browser page itself, so detaching is closing.
        """
        self.close()

    def __enter__(self) -> BrowserSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class BrowserShell:
    """复用启动/关闭外壳，同时失败关闭平台身份、端口与目录隔离。"""

    def __init__(
        self,
        registry: ProfileRegistry,
        *,
        options_factory: Callable[[], Any] | None = None,
        page_factory: Callable[[Any], Any] | None = None,
        port_selector: Callable[[str, str], int] = debug_port_for,
        port_available: Callable[[int], bool] | None = None,
        max_port_attempts: int = 4,
    ) -> None:
        self.registry = registry
        self.options_factory = options_factory or _default_options_factory
        self.page_factory = page_factory or _default_page_factory
        self.port_selector = port_selector
        self.port_available = port_available or _port_is_available
        if isinstance(max_port_attempts, bool) or not isinstance(max_port_attempts, int):
            raise BrowserShellError("invalid_port_attempts", "端口尝试次数必须是正整数。")
        if not 1 <= max_port_attempts <= 20:
            raise BrowserShellError("invalid_port_attempts", "端口尝试次数必须在 1 到 20 之间。")
        self.max_port_attempts = max_port_attempts

    @staticmethod
    def _translate_profile_error(exc: ProfileError) -> BrowserShellError:
        return BrowserShellError(exc.code, str(exc))

    @staticmethod
    def _cancelled(is_cancelled: Callable[[], bool] | None) -> bool:
        return bool(is_cancelled and is_cancelled())

    def open(
        self,
        spec: BrowserLaunchSpec,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> BrowserSession | None:
        if self._cancelled(is_cancelled):
            return None

        profile_lease: ProfileLease | None = None
        port_lease: DebugPortLease | None = None
        try:
            paths = self.registry.browser_paths(
                spec.platform_id,
                spec.profile_id,
                create=True,
            )
            profile_lease = self.registry.acquire(spec.platform_id, spec.profile_id)
            base_port = self.port_selector(spec.platform_id, spec.profile_id)
            if isinstance(base_port, bool) or not isinstance(base_port, int) or not 1024 <= base_port <= 65535:
                raise BrowserShellError(
                    "invalid_debug_port",
                    "浏览器调试端口必须在 1024 到 65535 之间。",
                )
            debug_port: int | None = None
            for attempt in range(self.max_port_attempts):
                candidate = base_port + attempt
                if candidate > 65535:
                    break
                try:
                    candidate_lease = self.registry.acquire_debug_port(
                        spec.platform_id,
                        spec.profile_id,
                        candidate,
                    )
                except ProfileError as exc:
                    if exc.code == "browser_port_busy":
                        continue
                    raise
                if self.port_available(candidate):
                    debug_port = candidate
                    port_lease = candidate_lease
                    break
                candidate_lease.release()
            if debug_port is None or port_lease is None:
                raise BrowserShellError("browser_port_busy", "浏览器调试端口不可用，已停止。")
            if self._cancelled(is_cancelled):
                return None

            options = self.options_factory()
            if spec.browser_path:
                options.set_browser_path(spec.browser_path)
            options.set_user_data_path(str(paths.user_data_dir))
            options.set_cache_path(str(paths.cache_dir))
            options.set_tmp_path(str(paths.temp_dir))
            options.set_download_path(str(paths.download_temp_dir))
            options.set_local_port(debug_port)
            options.set_argument("--no-first-run")
            close_timeout: float | None = None
            if paths.resolution.platform == "xiaohongshu":
                options.set_load_mode("eager")
                options.set_retry(times=0, interval=0)
                options.set_timeouts(
                    page_load=_XHS_PAGE_LOAD_TIMEOUT_SECONDS,
                    script=_XHS_SCRIPT_TIMEOUT_SECONDS,
                )
                close_timeout = _XHS_CLOSE_TIMEOUT_SECONDS
            page = self.page_factory(options)
            session = BrowserSession(
                page,
                paths,
                debug_port,
                profile_lease,
                port_lease,
                close_timeout=close_timeout,
            )
            profile_lease = None
            port_lease = None
            if self._cancelled(is_cancelled):
                session.close()
                return None
            return session
        except ProfileError as exc:
            raise self._translate_profile_error(exc) from exc
        except BrowserShellError:
            raise
        except Exception as exc:  # noqa: BLE001 - 启动边界统一失败关闭
            raise BrowserShellError("browser_launch_failed", f"浏览器启动失败:{exc}") from exc
        finally:
            if port_lease is not None:
                port_lease.release()
            if profile_lease is not None:
                profile_lease.release()


__all__ = [
    "BrowserLaunchSpec",
    "BrowserSession",
    "BrowserShell",
    "BrowserShellError",
    "debug_port_for",
]
