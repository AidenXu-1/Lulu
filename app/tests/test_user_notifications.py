import unittest

from src.core.user_notifications import (
    AuthorizationStatus,
    COMPLETION_BODY,
    COMPLETION_TITLE,
    FAILURE_BODY,
    FAILURE_TITLE,
    NotificationPolicy,
    PyObjCUserNotificationsAdapter,
    UserNotificationService,
)


class _Timer:
    def __init__(self, _seconds, callback):
        self.callback = callback
        self.cancelled = False
        self.started = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class _TimerFactory:
    def __init__(self):
        self.timers = []

    def __call__(self, seconds, callback):
        timer = _Timer(seconds, callback)
        self.timers.append(timer)
        return timer


class _Adapter:
    def __init__(self, status=AuthorizationStatus.AUTHORIZED):
        self.status = status
        self.status_callbacks = []
        self.request_callbacks = []
        self.schedule_callbacks = []
        self.scheduled = []
        self.raise_on_status = False

    def get_authorization_status(self, callback):
        if self.raise_on_status:
            raise RuntimeError("bridge unavailable")
        self.status_callbacks.append(callback)

    def request_authorization(self, callback):
        self.request_callbacks.append(callback)

    def schedule(self, title, body, identifier, *, play_sound, callback):
        self.scheduled.append((title, body, identifier, play_sound))
        self.schedule_callbacks.append(callback)

    def answer_status(self, status=None):
        self.status_callbacks.pop(0)(status or self.status)

    def answer_request(self, granted):
        self.request_callbacks.pop(0)(granted)

    def answer_schedule(self, succeeded):
        self.schedule_callbacks.pop(0)(succeeded)


class UserNotificationServiceTests(unittest.TestCase):
    def _service(self, adapter=None):
        self.timers = _TimerFactory()
        self.adapter = adapter or _Adapter()
        return UserNotificationService(
            adapter=self.adapter,
            timer_factory=self.timers,
            identifier_factory=lambda: "fixed-id",
            timeout_seconds=3,
        )

    def test_authorized_status_allows_enable_without_requesting_again(self):
        service = self._service()
        results = []
        service.request_enable(results.append)
        self.adapter.answer_status(AuthorizationStatus.AUTHORIZED)

        self.assertTrue(results[0].granted)
        self.assertEqual(results[0].status, AuthorizationStatus.AUTHORIZED)
        self.assertEqual(self.adapter.request_callbacks, [])

    def test_not_determined_requests_only_after_user_action(self):
        service = self._service(_Adapter(AuthorizationStatus.NOT_DETERMINED))
        self.assertEqual(self.adapter.status_callbacks, [])

        results = []
        service.request_enable(results.append)
        self.adapter.answer_status()
        self.assertEqual(len(self.adapter.request_callbacks), 1)
        self.adapter.answer_request(True)
        self.assertTrue(results[0].granted)

    def test_denied_exception_and_timeout_fail_closed(self):
        service = self._service(_Adapter(AuthorizationStatus.DENIED))
        denied = []
        service.request_enable(denied.append)
        self.adapter.answer_status()
        self.assertFalse(denied[0].granted)
        self.assertEqual(denied[0].status, AuthorizationStatus.DENIED)

        unavailable_adapter = _Adapter()
        unavailable_adapter.raise_on_status = True
        service = self._service(unavailable_adapter)
        unavailable = []
        service.request_enable(unavailable.append)
        self.assertFalse(unavailable[0].granted)
        self.assertEqual(unavailable[0].status, AuthorizationStatus.UNAVAILABLE)

        service = self._service(_Adapter())
        timed_out = []
        service.request_enable(timed_out.append)
        self.timers.timers[-1].fire()
        self.assertFalse(timed_out[0].granted)
        self.assertEqual(timed_out[0].status, AuthorizationStatus.TIMEOUT)

    def test_late_permission_callback_cannot_override_timeout(self):
        service = self._service(_Adapter())
        results = []
        service.request_enable(results.append)
        self.timers.timers[-1].fire()
        self.adapter.answer_status(AuthorizationStatus.AUTHORIZED)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, AuthorizationStatus.TIMEOUT)

    def test_never_and_inactive_while_frontmost_do_not_touch_bridge(self):
        service = self._service()
        results = []
        service.notify_task_completed(NotificationPolicy.NEVER, app_is_active=False, callback=results.append)
        service.notify_task_completed(NotificationPolicy.INACTIVE, app_is_active=True, callback=results.append)
        self.assertEqual(self.adapter.status_callbacks, [])
        self.assertEqual([item.reason for item in results], ["policy_never", "app_active"])

    def test_runtime_revocation_and_schedule_failure_never_affect_task(self):
        service = self._service(_Adapter(AuthorizationStatus.DENIED))
        results = []
        service.notify_task_completed(NotificationPolicy.ALWAYS, app_is_active=False, callback=results.append)
        self.adapter.answer_status()
        self.assertFalse(results[0].delivered)
        self.assertEqual(results[0].reason, "permission_denied")
        self.assertEqual(self.adapter.scheduled, [])

        service = self._service(_Adapter())
        results = []
        service.notify_task_completed(NotificationPolicy.ALWAYS, app_is_active=False, callback=results.append)
        self.adapter.answer_status()
        self.adapter.answer_schedule(False)
        self.assertFalse(results[0].delivered)
        self.assertEqual(results[0].reason, "schedule_failed")

    def test_scheduled_notification_uses_only_fixed_private_copy(self):
        service = self._service(_Adapter())
        results = []
        service.notify_task_completed(NotificationPolicy.ALWAYS, app_is_active=False, callback=results.append)
        self.adapter.answer_status()
        self.assertEqual(
            self.adapter.scheduled,
            [(COMPLETION_TITLE, COMPLETION_BODY, "fixed-id", True)],
        )
        self.assertNotIn("文件", COMPLETION_BODY)
        self.adapter.answer_schedule(True)
        self.assertTrue(results[0].delivered)

    def test_sound_switch_and_failure_use_fixed_private_copy(self):
        service = self._service(_Adapter())
        service.notify_task_completed(
            NotificationPolicy.ALWAYS,
            app_is_active=False,
            play_sound=False,
        )
        self.adapter.answer_status()
        self.assertEqual(
            self.adapter.scheduled,
            [(COMPLETION_TITLE, COMPLETION_BODY, "fixed-id", False)],
        )

        service = self._service(_Adapter())
        service.notify_task_failed(NotificationPolicy.ALWAYS, app_is_active=False)
        self.adapter.answer_status()
        self.assertEqual(
            self.adapter.scheduled,
            [(FAILURE_TITLE, FAILURE_BODY, "fixed-id", False)],
        )
        self.assertNotIn("文件", FAILURE_BODY)


class _Settings:
    def __init__(self, status):
        self._status = status

    def authorizationStatus(self):
        return self._status


class _Content:
    def __init__(self):
        self.title = ""
        self.body = ""
        self.sound = None

    def setTitle_(self, value):
        self.title = value

    def setBody_(self, value):
        self.body = value

    def setSound_(self, value):
        self.sound = value


class _Alloc:
    def __init__(self, value):
        self.value = value

    def init(self):
        return self.value


class _Center:
    def __init__(self):
        self.status = 0
        self.request_options = []
        self.added = []

    def getNotificationSettingsWithCompletionHandler_(self, callback):
        callback(_Settings(self.status))

    def requestAuthorizationWithOptions_completionHandler_(self, options, callback):
        self.request_options.append(options)
        callback(True, None)

    def addNotificationRequest_withCompletionHandler_(self, request, callback):
        self.added.append(request)
        callback(None)


class _FakeUN:
    UNAuthorizationStatusNotDetermined = 0
    UNAuthorizationStatusDenied = 1
    UNAuthorizationStatusAuthorized = 2
    UNAuthorizationStatusProvisional = 3
    UNAuthorizationStatusEphemeral = 4
    UNAuthorizationOptionAlert = 8
    UNAuthorizationOptionSound = 16

    def __init__(self):
        self.center = _Center()
        self.UNUserNotificationCenter = type(
            "CenterType", (), {"currentNotificationCenter": staticmethod(lambda: self.center)}
        )
        self.content = _Content()
        self.UNMutableNotificationContent = type(
            "ContentType", (), {"alloc": staticmethod(lambda: _Alloc(self.content))}
        )
        self.UNNotificationRequest = type(
            "RequestType",
            (),
            {
                "requestWithIdentifier_content_trigger_": staticmethod(
                    lambda identifier, content, trigger: (
                        identifier,
                        content.title,
                        content.body,
                        trigger,
                    )
                )
            },
        )
        self.UNNotificationSound = type(
            "SoundType", (), {"defaultSound": staticmethod(lambda: "default-sound")}
        )


class PyObjCAdapterContractTests(unittest.TestCase):
    def test_adapter_uses_user_notifications_selectors_without_real_framework(self):
        framework = _FakeUN()
        adapter = PyObjCUserNotificationsAdapter(framework_loader=lambda: framework)

        statuses = []
        framework.center.status = framework.UNAuthorizationStatusAuthorized
        adapter.get_authorization_status(statuses.append)
        self.assertEqual(statuses, [AuthorizationStatus.AUTHORIZED])

        granted = []
        adapter.request_authorization(granted.append)
        self.assertEqual(granted, [True])
        self.assertEqual(
            framework.center.request_options,
            [framework.UNAuthorizationOptionAlert | framework.UNAuthorizationOptionSound],
        )

        sent = []
        adapter.schedule(
            COMPLETION_TITLE,
            COMPLETION_BODY,
            "id",
            play_sound=True,
            callback=sent.append,
        )
        self.assertEqual(sent, [True])
        self.assertEqual(framework.center.added[0][:3], ("id", COMPLETION_TITLE, COMPLETION_BODY))
        self.assertEqual(framework.content.sound, "default-sound")

    def test_native_registration_exception_releases_held_callback(self):
        framework = _FakeUN()

        def fail_registration(_callback):
            raise RuntimeError("native registration failed")

        framework.center.getNotificationSettingsWithCompletionHandler_ = fail_registration
        adapter = PyObjCUserNotificationsAdapter(framework_loader=lambda: framework)
        with self.assertRaises(RuntimeError):
            adapter.get_authorization_status(lambda _status: None)
        self.assertEqual(adapter._callbacks, set())


if __name__ == "__main__":
    unittest.main()
