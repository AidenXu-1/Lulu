import unittest
from types import SimpleNamespace

from src.core.completion_feedback import CompletionFeedback, CompletionTarget


class _Notifications:
    def __init__(self):
        self.completed = []
        self.failed = []
        self.raise_on_call = False

    def notify_task_completed(self, policy, **kwargs):
        if self.raise_on_call:
            raise RuntimeError("bridge failed")
        self.completed.append((policy, kwargs))

    def notify_task_failed(self, policy, **kwargs):
        if self.raise_on_call:
            raise RuntimeError("bridge failed")
        self.failed.append((policy, kwargs))


class CompletionFeedbackTests(unittest.TestCase):
    def _feedback(self, **overrides):
        cfg = SimpleNamespace(
            notification_policy="inactive",
            notify_on_failure=True,
            notification_sound_enabled=True,
            completion_action="stay",
        )
        for key, value in overrides.items():
            setattr(cfg, key, value)
        notifications = _Notifications()
        opened = []
        feedback = CompletionFeedback(cfg, notifications, opened.append)
        return feedback, notifications, opened

    def test_success_keeps_page_by_default_and_controls_notification_sound(self):
        feedback, notifications, opened = self._feedback()
        target = CompletionTarget(page_index=3)
        feedback.success(target, app_is_active=False)
        self.assertEqual(opened, [])
        self.assertEqual(
            notifications.completed,
            [("inactive", {"app_is_active": False, "play_sound": True})],
        )

        feedback, notifications, _opened = self._feedback(notification_sound_enabled=False)
        feedback.success(target, app_is_active=True)
        self.assertFalse(notifications.completed[0][1]["play_sound"])

    def test_open_result_only_navigates_when_target_is_real(self):
        feedback, _notifications, opened = self._feedback(completion_action="open_result")
        target = CompletionTarget(page_index=2, library_kind="transcript", entry_id="entry-1")
        feedback.success(target, app_is_active=False)
        self.assertEqual(opened, [target])
        feedback.success(None, app_is_active=False)
        self.assertEqual(opened, [target])

    def test_failure_respects_switch_and_never_raises_into_business_task(self):
        feedback, notifications, _opened = self._feedback()
        feedback.failure(app_is_active=False)
        self.assertEqual(
            notifications.failed,
            [("inactive", {"app_is_active": False})],
        )

        feedback, notifications, _opened = self._feedback(notify_on_failure=False)
        feedback.failure(app_is_active=False)
        self.assertEqual(notifications.failed, [])

        feedback, notifications, _opened = self._feedback()
        notifications.raise_on_call = True
        feedback.success(CompletionTarget(0), app_is_active=False)
        feedback.failure(app_is_active=False)


if __name__ == "__main__":
    unittest.main()
