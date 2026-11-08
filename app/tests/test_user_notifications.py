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


    def test_status_check_never_prompts_even_if_not_determined(self):
        service = self._service(_Adapter(AuthorizationStatus.NOT_DETERMINED))
        results = []
        service.check_authorization(results.append)
        self.adapter.answer_status()
        self.assertEqual(results, [AuthorizationStatus.NOT_DETERMINED])
        self.assertEqual(self.adapter.request_callbacks, [])
        self.assertTrue(self.timers.timers[-1].cancelled)









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








if __name__ == "__main__":
    unittest.main()
