"""长任务期间的 macOS 防休眠资源管理。"""

from __future__ import annotations

import subprocess
import threading
from typing import Protocol


class _PowerAdapter(Protocol):
    def start(self): ...
    def stop(self, process) -> None: ...
    def running(self, process) -> bool: ...


class CaffeinateAdapter:
    """只持有 idle-system-sleep assertion，不强制点亮屏幕。"""

    def __init__(self, *, process_factory=subprocess.Popen):
        self._process_factory = process_factory

    def start(self):
        return self._process_factory(
            ["/usr/bin/caffeinate", "-i"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    def stop(self, process) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1.0)
            except Exception:  # noqa: BLE001 - 退出阶段不能阻塞 App 关闭
                pass

    def running(self, process) -> bool:
        return process is not None and process.poll() is None


class PowerManager:
    """用 owner 集合保证并发任务不会提前释放系统 assertion。"""

    def __init__(self, *, adapter: _PowerAdapter | None = None, enabled: bool = False):
        self._adapter = adapter or CaffeinateAdapter()
        self._enabled = bool(enabled)
        self._owners: set[str] = set()
        self._process = None
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def active_owners(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._owners))

    @property
    def is_asserting(self) -> bool:
        with self._lock:
            return self._is_running()

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            self._enabled = bool(enabled)
            if not self._enabled:
                self._stop()
                return True
            return self._ensure_started() if self._owners else True

    def acquire(self, owner: str) -> bool:
        key = str(owner or "task")
        with self._lock:
            self._owners.add(key)
            if not self._enabled:
                return True
            return self._ensure_started()

    def release(self, owner: str) -> None:
        key = str(owner or "task")
        with self._lock:
            self._owners.discard(key)
            if not self._owners:
                self._stop()

    def sync_activity(self, active: bool, *, owner: str = "main-window") -> bool:
        if active:
            return self.acquire(owner)
        self.release(owner)
        return True

    def shutdown(self) -> None:
        with self._lock:
            self._owners.clear()
            self._stop()

    def _is_running(self) -> bool:
        if self._process is None:
            return False
        try:
            running = bool(self._adapter.running(self._process))
        except Exception:  # noqa: BLE001 - 适配器异常按未持有处理
            running = False
        if not running:
            self._process = None
        return running

    def _ensure_started(self) -> bool:
        if self._is_running():
            return True
        try:
            self._process = self._adapter.start()
            return self._is_running()
        except Exception:  # noqa: BLE001 - 防休眠失败不能伤害业务任务
            self._process = None
            return False

    def _stop(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            self._adapter.stop(process)
        except Exception:  # noqa: BLE001 - 退出继续，OS 会随父进程清理子进程
            pass
