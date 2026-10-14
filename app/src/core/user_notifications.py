"""任务完成系统通知：策略、权限与可注入的 macOS 原生桥。"""

from __future__ import annotations

import sys
import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

COMPLETION_TITLE = "任务已完成"
COMPLETION_BODY = "Lulu 已完成一个处理任务。"
FAILURE_TITLE = "任务处理失败"
FAILURE_BODY = "Lulu 有一个任务未能完成。"


class AuthorizationStatus(str, Enum):
    NOT_DETERMINED = "not_determined"
    DENIED = "denied"
    AUTHORIZED = "authorized"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    TIMEOUT = "timeout"


class NotificationPolicy(str, Enum):
    ALWAYS = "always"
    INACTIVE = "inactive"
    NEVER = "never"


@dataclass(frozen=True)
class PermissionResult:
    granted: bool
    status: AuthorizationStatus
    reason: str


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    reason: str


class UserNotificationsAdapter(Protocol):
    def get_authorization_status(self, callback: Callable[[AuthorizationStatus], None]) -> None: ...
    def request_authorization(self, callback: Callable[[bool], None]) -> None: ...
    def schedule(
        self,
        title: str,
        body: str,
        identifier: str,
        *,
        play_sound: bool,
        callback: Callable[[bool], None],
    ) -> None: ...


def _import_user_notifications():
    if sys.platform != "darwin":
        raise RuntimeError("UserNotifications 只在 macOS 可用")
    import UserNotifications  # type: ignore[import-not-found]

    return UserNotifications


class PyObjCUserNotificationsAdapter:
    """Apple UserNotifications 的窄桥；框架在首次调用时才导入。"""

    def __init__(self, *, framework_loader: Callable[[], object] | None = None):
        self._framework_loader = framework_loader or _import_user_notifications
        self._framework = None
        self._callbacks: set[Callable] = set()
        self._callback_lock = threading.RLock()

    def _un(self):
        if self._framework is None:
            framework = self._framework_loader()
            required = (
                "UNUserNotificationCenter",
                "UNMutableNotificationContent",
                "UNNotificationRequest",
            )
            if any(not hasattr(framework, name) for name in required):
                raise RuntimeError("UserNotifications bridge 不完整")
            self._framework = framework
        return self._framework

    def _held(self, callback: Callable) -> Callable:
        def wrapped(*args):
            try:
                callback(*args)
            finally:
                with self._callback_lock:
                    self._callbacks.discard(wrapped)

        with self._callback_lock:
            self._callbacks.add(wrapped)
        return wrapped

    def _discard_held(self, callback: Callable) -> None:
        with self._callback_lock:
            self._callbacks.discard(callback)

    def get_authorization_status(self, callback: Callable[[AuthorizationStatus], None]) -> None:
        un = self._un()
        center = un.UNUserNotificationCenter.currentNotificationCenter()

        def completed(settings) -> None:
            try:
                raw = settings.authorizationStatus()
                authorized_values = {
                    getattr(un, "UNAuthorizationStatusAuthorized", object()),
                    getattr(un, "UNAuthorizationStatusProvisional", object()),
                    getattr(un, "UNAuthorizationStatusEphemeral", object()),
                }
                if raw in authorized_values:
                    status = AuthorizationStatus.AUTHORIZED
                elif raw == getattr(un, "UNAuthorizationStatusNotDetermined", object()):
                    status = AuthorizationStatus.NOT_DETERMINED
                elif raw == getattr(un, "UNAuthorizationStatusDenied", object()):
                    status = AuthorizationStatus.DENIED
                else:
                    status = AuthorizationStatus.ERROR
            except Exception:  # noqa: BLE001 - 原生返回异常按失败关闭
                status = AuthorizationStatus.ERROR
            callback(status)

        held = self._held(completed)
        try:
            center.getNotificationSettingsWithCompletionHandler_(held)
        except Exception:
            self._discard_held(held)
            raise

    def request_authorization(self, callback: Callable[[bool], None]) -> None:
        un = self._un()
        center = un.UNUserNotificationCenter.currentNotificationCenter()

        def completed(granted, error) -> None:
            callback(bool(granted) and error is None)

        options = un.UNAuthorizationOptionAlert | getattr(un, "UNAuthorizationOptionSound", 0)
        held = self._held(completed)
        try:
            center.requestAuthorizationWithOptions_completionHandler_(options, held)
        except Exception:
            self._discard_held(held)
            raise

    def schedule(
        self,
        title: str,
        body: str,
        identifier: str,
        *,
        play_sound: bool,
        callback: Callable[[bool], None],
    ) -> None:
        un = self._un()
        center = un.UNUserNotificationCenter.currentNotificationCenter()
        content = un.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(body)
        if play_sound:
            sound_type = getattr(un, "UNNotificationSound", None)
            if sound_type is None or not hasattr(content, "setSound_"):
                raise RuntimeError("UserNotifications sound bridge 不完整")
            content.setSound_(sound_type.defaultSound())
        request = un.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            identifier, content, None
        )

        def completed(error) -> None:
            callback(error is None)

        held = self._held(completed)
        try:
            center.addNotificationRequest_withCompletionHandler_(request, held)
        except Exception:
            self._discard_held(held)
            raise


class UserNotificationService:
    """只暴露启用许可和固定隐私文案的完成提醒。"""

    def __init__(
        self,
        *,
        adapter: UserNotificationsAdapter | None = None,
        timer_factory: Callable[[float, Callable[[], None]], object] = threading.Timer,
        identifier_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        timeout_seconds: float = 8.0,
    ):
        self._adapter = adapter or PyObjCUserNotificationsAdapter()
        self._timer_factory = timer_factory
        self._identifier_factory = identifier_factory
        self._timeout_seconds = float(timeout_seconds)

    def request_enable(self, callback: Callable[[PermissionResult], None]) -> None:
        timeout = PermissionResult(False, AuthorizationStatus.TIMEOUT, "permission_timeout")
        unavailable = PermissionResult(False, AuthorizationStatus.UNAVAILABLE, "bridge_unavailable")

        def start(finish: Callable[[PermissionResult], None]) -> None:
            def status_ready(raw_status) -> None:
                status = _authorization_status(raw_status)
                if status == AuthorizationStatus.AUTHORIZED:
                    finish(PermissionResult(True, status, "authorized"))
                elif status == AuthorizationStatus.NOT_DETERMINED:
                    try:
                        self._adapter.request_authorization(
                            lambda granted: finish(
                                PermissionResult(
                                    bool(granted),
                                    AuthorizationStatus.AUTHORIZED if granted else AuthorizationStatus.DENIED,
                                    "authorized" if granted else "permission_denied",
                                )
                            )
                        )
                    except Exception:  # noqa: BLE001
                        finish(unavailable)
                elif status == AuthorizationStatus.DENIED:
                    finish(PermissionResult(False, status, "permission_denied"))
                else:
                    finish(unavailable)

            self._adapter.get_authorization_status(status_ready)

        self._run_once(start, timeout=timeout, failed=unavailable, callback=callback)

    def notify_task_completed(
        self,
        policy: NotificationPolicy | str,
        *,
        app_is_active: bool,
        play_sound: bool = True,
        callback: Callable[[DeliveryResult], None] | None = None,
    ) -> None:
        self._notify(
            policy,
            app_is_active=app_is_active,
            title=COMPLETION_TITLE,
            body=COMPLETION_BODY,
            play_sound=bool(play_sound),
            callback=callback,
        )

    def notify_task_failed(
        self,
        policy: NotificationPolicy | str,
        *,
        app_is_active: bool,
        callback: Callable[[DeliveryResult], None] | None = None,
    ) -> None:
        self._notify(
            policy,
            app_is_active=app_is_active,
            title=FAILURE_TITLE,
            body=FAILURE_BODY,
            play_sound=False,
            callback=callback,
        )

    def _notify(
        self,
        policy: NotificationPolicy | str,
        *,
        app_is_active: bool,
        title: str,
        body: str,
        play_sound: bool,
        callback: Callable[[DeliveryResult], None] | None,
    ) -> None:
        done = callback or (lambda _result: None)
        normalized = _notification_policy(policy)
        if normalized == NotificationPolicy.NEVER:
            done(DeliveryResult(False, "policy_never"))
            return
        if normalized == NotificationPolicy.INACTIVE and app_is_active:
            done(DeliveryResult(False, "app_active"))
            return

        timeout = DeliveryResult(False, "permission_timeout")
        unavailable = DeliveryResult(False, "permission_unavailable")

        def start(finish: Callable[[DeliveryResult], None]) -> None:
            def status_ready(raw_status) -> None:
                status = _authorization_status(raw_status)
                if status == AuthorizationStatus.DENIED:
                    finish(DeliveryResult(False, "permission_denied"))
                    return
                if status != AuthorizationStatus.AUTHORIZED:
                    finish(unavailable)
                    return
                try:
                    self._adapter.schedule(
                        title,
                        body,
                        self._identifier_factory(),
                        play_sound=play_sound,
                        callback=lambda succeeded: finish(
                            DeliveryResult(bool(succeeded), "delivered" if succeeded else "schedule_failed")
                        ),
                    )
                except Exception:  # noqa: BLE001
                    finish(DeliveryResult(False, "schedule_failed"))

            self._adapter.get_authorization_status(status_ready)

        self._run_once(start, timeout=timeout, failed=unavailable, callback=done)

    def _run_once(self, start, *, timeout, failed, callback) -> None:
        lock = threading.RLock()
        state = {"finished": False}
        timer_box = {"timer": None}

        def finish(result) -> None:
            with lock:
                if state["finished"]:
                    return
                state["finished"] = True
                timer = timer_box["timer"]
                if timer is not None:
                    try:
                        timer.cancel()
                    except Exception:  # noqa: BLE001
                        pass
            callback(result)

        timer = self._timer_factory(self._timeout_seconds, lambda: finish(timeout))
        timer_box["timer"] = timer
        try:
            if hasattr(timer, "daemon"):
                timer.daemon = True
            timer.start()
            start(finish)
        except Exception:  # noqa: BLE001 - 原生桥异常统一失败关闭
            finish(failed)


def _authorization_status(value) -> AuthorizationStatus:
    try:
        return value if isinstance(value, AuthorizationStatus) else AuthorizationStatus(str(value))
    except (TypeError, ValueError):
        return AuthorizationStatus.ERROR


def _notification_policy(value) -> NotificationPolicy:
    try:
        return value if isinstance(value, NotificationPolicy) else NotificationPolicy(str(value))
    except (TypeError, ValueError):
        return NotificationPolicy.NEVER
