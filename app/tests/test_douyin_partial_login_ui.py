"""Explicit, bounded login retry from an anonymous homepage failure.

Uses an offscreen page, in-memory settings, fake workers and a fake service.
There is no browser, real configuration, account lookup or network request.
"""

import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from src.core.collect.application import CollectionCommand
from src.core.collect.contracts import AdapterCapability, CollectResult
from src.core.collect.models import CollectedItem
from src.core.collect.router import detect_link
from src.ui.collection_page import CollectionPage


URL = "https://www.douyin.com/user/MS4w.fixture?from_tab_name=main&vid=123"
OTHER_URL = "https://www.douyin.com/user/MS4w.other"


class PureService:
    def inspect(self, text):
        identity = detect_link(text)
        capability = AdapterCapability(
            identity.platform,
            identity.platform == "douyin",
            "existing_adapter_ready" if identity.platform == "douyin" else "release_disabled",
            ("anonymous_latest", "profile_latest", "profile_all"),
        )
        return SimpleNamespace(identity=identity, capability=capability,
                               allowed_modes=capability.modes)


class PendingWorker:
    running = True

    def isRunning(self):
        return self.running


class DouyinPartialLoginUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.login_patch = patch("src.ui.collection_page.is_logged_in",
                                 side_effect=AssertionError("real login lookup"))
        self.login_patch.start()
        self.confirm_patch = patch("src.ui.collection_page.dialogs.confirm", return_value=False)
        self.confirm = self.confirm_patch.start()
        cfg = SimpleNamespace(collection_mode="partial", collect_cover=False,
                              feishu_target={}, save=lambda: True)
        self.page = CollectionPage(
            cfg,
            collection_service=PureService(),
            douyin_login_state=lambda: ("未登录", False),
        )
        self.page.input.setText(URL)
        self.page.resize(1200, 760)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        # Every worker in this test is a non-running double, never a QThread.
        self.page._worker = None
        self.page._full_worker = None
        self.page.shutdown_workers()
        self.page.deleteLater()
        self.app.processEvents()
        self.confirm_patch.stop()
        self.login_patch.stop()

    def deliver(self, reason="permission_denied", *, limit=7,
                mode="anonymous_latest", result=None):
        self.page._run_token += 1
        token = self.page._run_token
        command = CollectionCommand(URL, mode, limit=limit)
        self.page._history_context[token] = (command, detect_link(URL))
        self.page._active_collection_mode = "partial"
        worker = PendingWorker()
        self.page._worker = worker
        self.page._on_collect_done(result or CollectResult.failed(reason), token)
        return token, worker, command

    def finish(self, worker):
        worker.running = False
        self.page._on_collect_worker_finished(worker, False)
        self.app.processEvents()


    def test_manual_login_progress_uses_safe_fixed_instructions(self):
        token = 7
        self.page._run_token = token
        self.page._history_context[token] = (CollectionCommand(URL, "profile_latest", limit=3), detect_link(URL))
        self.page._on_collect_progress(token, 0, "请在 Lulu 专用浏览器中手动登录 Cookie=synthetic")
        self.assertIn("手动登录", self.page.status.text())
        self.assertNotIn("synthetic", self.page.status.text())
        self.page._on_collect_progress(token, 0, "等待你在弹出的浏览器窗口完成验证 Cookie=synthetic")
        self.assertIn("自行完成验证", self.page.status.text())
        self.assertNotIn("synthetic", self.page.status.text())
        self.page._on_collect_progress(token, 0, "private-token=synthetic")
        self.assertNotIn("synthetic", self.page.status.text())

    def test_rejected_request_requires_action_after_worker_finished(self):
        token, worker, command = self.deliver()
        self.assertFalse(self.page.partial_login_btn.isHidden())
        self.assertFalse(self.page.partial_login_btn.isEnabled())
        self.confirm.assert_not_called()
        with patch.object(self.page, "_start_collect") as start:
            self.page._on_partial_login_retry()
            start.assert_not_called()
            self.confirm.assert_not_called()
            self.finish(worker)
            self.assertTrue(self.page.partial_login_btn.isEnabled())
            start.assert_not_called()
            self.confirm.return_value = True
            self.page.partial_login_btn.click()
        retry = start.call_args.args[0]
        self.assertEqual(retry.raw_input, command.raw_input)
        self.assertEqual(retry.mode, "profile_latest")
        self.assertEqual(retry.limit, 7)
        self.assertFalse(start.call_args.kwargs["full"])
        self.assertEqual(self.page._selected_mode, "partial")
        self.assertIsNone(self.page._partial_login_retry)
        copy = self.confirm.call_args.args[2]
        for phrase in ("专用浏览器", "可复用已保存登录", "本人", "不会全量滚动", "7"):
            self.assertIn(phrase, copy)
        self.assertEqual(self.confirm.call_args.kwargs["ok"], "登录后部分采集")
        self.assertEqual(self.confirm.call_args.kwargs["cancel"], "取消")

    def test_cancel_never_starts_or_repeats_collection(self):
        _, worker, _ = self.deliver()
        self.finish(worker)
        with patch.object(self.page, "_start_collect") as start:
            self.page.partial_login_btn.click()
            self.page._on_partial_login_retry()
        start.assert_not_called()
        self.assertEqual(self.confirm.call_count, 1)
        self.assertIsNone(self.page._partial_login_retry)
        self.assertTrue(self.page.partial_login_btn.isHidden())


    def test_frozen_document_limit_is_bounded_without_expansion(self):
        for limit, expected in ((3, 3), (20, 20), (100, 20), (0, 1), (None, 20)):
            with self.subTest(limit=limit):
                _, worker, _ = self.deliver(limit=limit)
                self.finish(worker)
                self.confirm.return_value = True
                with patch.object(self.page, "_start_collect") as start:
                    self.page.partial_login_btn.click()
                self.assertEqual(start.call_args.args[0].limit, expected)





    def test_context_change_while_confirmation_is_open_cannot_start_retry(self):
        _, worker, _ = self.deliver()
        self.finish(worker)
        def changed(*_args, **_kwargs):
            self.page.input.setText(OTHER_URL)
            return True
        self.confirm.side_effect = changed
        with patch.object(self.page, "_start_collect") as start:
            self.page.partial_login_btn.click()
        start.assert_not_called()



if __name__ == "__main__":
    unittest.main()
