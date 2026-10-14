import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAccessible, QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from src.core.updater.models import UpdateSnapshot
from src.core.user_notifications import AuthorizationStatus, PermissionResult
from src.ui import theme as T
from src.ui.consumer_select import ConsumerSelect
from src.ui.settings_page import AuthorContactDialog, SettingsPage


class _Controller(QObject):
    changed = Signal(object)
    install_accepted = Signal()

    def __init__(self):
        super().__init__()
        self.snapshot = UpdateSnapshot(current_version="0.2.4", current_build="20260712.1")
        self.check_calls = []

    def check(self, *, manual):
        self.check_calls.append(bool(manual))

    def download(self):
        return None

    def cancel(self):
        return None

    def install_gate(self):
        return "ok"

    def publish(self):
        self.changed.emit(self.snapshot)

    def shutdown(self, _wait_ms=0):
        return True


class _NotificationService:
    def __init__(self):
        self.callbacks = []
        self.raise_on_request = False

    def request_enable(self, callback):
        if self.raise_on_request:
            raise RuntimeError("native bridge unavailable")
        self.callbacks.append(callback)

    def answer(self, *, granted, status, reason):
        self.callbacks.pop(0)(PermissionResult(granted, status, reason))


class _Cfg:
    def __init__(self, root: Path):
        self.theme = "warm"
        self.last_output_dir = str(root / "exports")
        self.media_dir = str(root / "media")
        self.recording_dir = str(root / "recordings")
        self.tts_output_dir = str(root / "voices")
        self.voice_library_dir = str(root / "voice-library")
        self.models_dir = str(root / "models")
        self.auto_update_enabled = True
        self.notification_policy = "never"
        self.notify_on_failure = True
        self.notification_sound_enabled = True
        self.prevent_sleep_during_tasks = False
        self.confirm_exit_during_tasks = True
        self.completion_action = "stay"
        self.save_result = True
        self.save_calls = 0

    def save(self):
        self.save_calls += 1
        return self.save_result


class SettingsPageTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _Cfg(self.root)
        self.notifications = _NotificationService()
        self.controller = _Controller()
        # Keep every settings/shell probe away from the real profile and library.
        from src.core import library, models_manager
        from src.core.agent_workspace import skills
        for boundary in (
            patch.object(Path, "home", return_value=self.root/"home"),
            patch.object(library, "LIBRARY_DIR", self.root/"library"),
            patch.object(library, "FOLDERS_FILE", self.root/"library/folders.json"),
            patch.object(models_manager, "app_data_dir", return_value=self.root),
            patch.object(skills, "skill_root", return_value=self.root/"skills"),
        ):
            boundary.start()
            self.addCleanup(boundary.stop)

    def tearDown(self):
        T.apply_theme("warm")
        self.tmp.cleanup()

    def _page(self, notification_service=True, *, external_storage_busy=None):
        return SettingsPage(
            self.cfg,
            update_controller=self.controller,
            notification_service=self.notifications if notification_service else None,
            external_storage_busy=external_storage_busy,
        )

    def _wait_skill(self, page):
        limit = time.monotonic() + 2
        while page.agent_skill_state == "checking" and time.monotonic() < limit:
            QTest.qWait(5)
        self.assertNotEqual(page.agent_skill_state, "checking")

    @staticmethod
    def _visible_copy(widget) -> str:
        return " ".join(label.text() for label in widget.findChildren(QLabel))

    def test_page_titlebar_immersive_offscreen_never_loads_objc(self):
        from src.ui import macos

        class _Window:
            @staticmethod
            def winId():  # noqa: N802
                return 1

        self.assertEqual(QGuiApplication.platformName(), "offscreen")
        with patch.object(macos.sys, "platform", "darwin"), patch.object(
            macos.ctypes.cdll,
            "LoadLibrary",
            side_effect=AssertionError("offscreen must not touch Objective-C"),
        ) as load_objc:
            self.assertFalse(macos.set_page_titlebar_immersive(_Window(), True))
        load_objc.assert_not_called()

    def test_page_titlebar_immersive_without_gui_application_never_loads_objc(self):
        from src.ui import macos

        with patch.object(macos.sys, "platform", "darwin"), patch.object(
            QGuiApplication,
            "instance",
            return_value=None,
        ), patch.object(
            macos.ctypes.cdll,
            "LoadLibrary",
            side_effect=AssertionError("missing Qt app must not touch Objective-C"),
        ) as load_objc:
            self.assertFalse(macos.set_page_titlebar_immersive(object(), True))
        load_objc.assert_not_called()

    def test_page_titlebar_immersive_cocoa_keeps_existing_bridge_contract(self):
        from src.ui import macos

        objc = SimpleNamespace(
            sel_registerName=MagicMock(side_effect=lambda name: name),
            objc_msgSend=MagicMock(),
        )

        def send(_receiver, selector, *_args):
            if selector == b"window":
                return 20260722
            if selector == b"respondsToSelector:":
                return True
            if selector == b"titlebarSeparatorStyle":
                return 7
            if selector == b"styleMask":
                return 0x10
            return 0

        objc.objc_msgSend.side_effect = send

        class _Window:
            @staticmethod
            def winId():  # noqa: N802
                return 101

        window = _Window()
        with patch.object(macos.sys, "platform", "darwin"), patch.object(
            QGuiApplication,
            "platformName",
            return_value="cocoa",
        ), patch.object(macos.ctypes.cdll, "LoadLibrary", return_value=objc):
            self.assertTrue(macos.set_page_titlebar_immersive(window, True))
            self.assertEqual(window._lulu_titlebar_separator_style, 7)
            self.assertTrue(macos.set_page_titlebar_immersive(window, False))

        selectors = [call.args[1] for call in objc.objc_msgSend.call_args_list]
        self.assertIn(b"setTitlebarAppearsTransparent:", selectors)
        self.assertIn(b"setStyleMask:", selectors)
        self.assertEqual(selectors.count(b"setTitlebarSeparatorStyle:"), 2)

    def test_current_categories_include_feishu_and_startup_setting_stays_removed(self):
        page = self._page()
        self.assertEqual(
            page.category_order,
            ("general", "storage", "skills", "cli", "feishu", "about"),
        )
        self.assertEqual(
            [page.category_buttons[key].text() for key in page.category_order],
            ["通用", "文件与存储", "技能", "CLI", "飞书知识库", "关于"],
        )
        self.assertFalse(hasattr(page, "startup_buttons"))
        self.assertNotIn("启动时打开", self._visible_copy(page))
        self.assertNotIn("上次使用页面", self._visible_copy(page))

    def test_agent_skill_lives_in_dedicated_cli_page(self):
        page = self._page()

        visible_labels = self._visible_copy(page)
        visible_buttons = " ".join(
            button.text() for button in page.findChildren(QPushButton)
        )
        visible_copy = f"{visible_labels} {visible_buttons}"

        self.assertNotIn("agent", page._all_category_order)
        self.assertNotIn("agent", page.category_buttons)
        for removed_copy in (
            "Agent 调用",
            "Agent 接入",
            "安装 Agent 接入",
            "修复 Agent 接入",
            "更新 Agent 接入",
            "关闭 Agent 接入",
            "Agent 接入已就绪",
        ):
            self.assertNotIn(removed_copy, visible_copy)

        self.assertIn("Lulu Agent Skill", visible_labels)
        self.assertIn("让支持 Skill 的 Agent 通过 Lulu 处理任务。", visible_labels)
        self._wait_skill(page)
        self.assertEqual(page.agent_skill_status_label.text(), "尚未安装")
        self.assertEqual(page.agent_skill_install_button.text(), "安装 Lulu Agent Skill")
        self.assertTrue(
            page.detail_scrolls["cli"].isAncestorOf(page.agent_skill_status_label)
        )

        self.assertTrue(page.detail_scrolls["cli"].isAncestorOf(page.tools_cli_button))
        page.tools_cli_button.click()
        self.app.processEvents()
        from src.ui.tools_cli_dialog import ToolsCliDialog
        dialog = page.findChild(ToolsCliDialog)
        self.assertIsNotNone(dialog)
        text = dialog.reference.toPlainText()
        for content in ("lulu capabilities --output json", "lulu tts status --output json",
                        "IndexTTS 2.5", "IndexTTS 2", "VoxCPM2", "Qwen3-TTS"):
            self.assertIn(content, text)
        dialog.close()

    def test_agent_skill_shows_update_for_a_new_current_app_bundle(self):
        page = SettingsPage(
            self.cfg,
            update_controller=self.controller,
            notification_service=self.notifications,
            agent_skill_status_provider=lambda: "update_available",
        )

        self._wait_skill(page)
        self.assertEqual(page.agent_skill_state, "update_available")
        self.assertEqual(
            page.agent_skill_status_label.text(),
            "可更新为当前 Lulu 的配套版本",
        )
        self.assertEqual(
            page.agent_skill_install_button.text(),
            "更新 Lulu Agent Skill",
        )
        self.assertFalse(page.agent_skill_install_button.isHidden())

    def test_general_uses_consumer_select_and_contains_all_confirmed_settings(self):
        page = self._page()
        self.assertIsInstance(page.notification_select, ConsumerSelect)
        self.assertEqual(page.notification_select.currentData(), "never")
        self.assertTrue(page.failure_notification_switch.isChecked())
        self.assertTrue(page.notification_sound_switch.isChecked())
        self.assertFalse(page.prevent_sleep_switch.isChecked())
        self.assertTrue(page.exit_protection_switch.isChecked())
        self.assertIsInstance(page.completion_action_select, ConsumerSelect)
        self.assertEqual(page.completion_action_select.currentData(), "stay")
        self.assertEqual(self.notifications.callbacks, [])
        visible = self._visible_copy(page)
        for text in (
            "任务完成提醒",
            "失败任务也提醒",
            "任务完成提示音",
            "防止 Mac 自动休眠",
            "运行中退出保护",
            "任务完成后的行为",
        ):
            self.assertIn(text, visible)

    def test_general_switches_and_completion_action_save_and_roll_back(self):
        page = self._page()
        changed = []
        page.preferences_changed.connect(changed.append)

        page.failure_notification_switch.click()
        page.notification_sound_switch.click()
        page.exit_protection_switch.click()
        page.completion_action_select.set_value("open_result", emit=True)

        self.assertFalse(self.cfg.notify_on_failure)
        self.assertFalse(self.cfg.notification_sound_enabled)
        self.assertFalse(self.cfg.confirm_exit_during_tasks)
        self.assertEqual(self.cfg.completion_action, "open_result")
        self.assertTrue({
            "notify_on_failure", "notification_sound_enabled",
            "confirm_exit_during_tasks", "completion_action",
        }.issubset(set(changed)))

        self.cfg.save_result = False
        with patch("src.ui.settings_page.dialogs.alert"):
            page.exit_protection_switch.click()
            page.completion_action_select.set_value("stay", emit=True)
        self.assertFalse(self.cfg.confirm_exit_during_tasks)
        self.assertFalse(page.exit_protection_switch.isChecked())
        self.assertEqual(self.cfg.completion_action, "open_result")
        self.assertEqual(page.completion_action_select.currentData(), "open_result")

    def test_real_failed_switch_save_reverses_visibly_before_restoring_endpoint(self):
        page = self._page()
        page.resize(900, 720)
        page.show()
        self.app.processEvents()
        switch = page.exit_protection_switch
        self.assertTrue(switch.isChecked())
        self.assertAlmostEqual(switch.position, 1.0, places=3)
        self.cfg.save_result = False

        with patch("src.ui.settings_page.dialogs.alert"):
            QTest.mouseClick(switch, Qt.LeftButton)
        self.assertTrue(switch.isChecked())
        self.assertTrue(self.cfg.confirm_exit_during_tasks)
        QTest.qWait(55)
        self.assertGreater(switch.position, 0.0)
        self.assertLess(switch.position, 1.0)
        QTest.qWait(180)
        self.assertAlmostEqual(switch.position, 1.0, places=3)
        page.close()

    def test_general_switch_labels_are_real_accessibility_buddies(self):
        page = self._page()
        controls = {
            "失败任务也提醒": page.failure_notification_switch,
            "任务完成提示音": page.notification_sound_switch,
            "处理任务时防止 Mac 自动休眠": page.prevent_sleep_switch,
            "运行中退出保护": page.exit_protection_switch,
        }
        labels = {label.text(): label for label in page.findChildren(QLabel, "fieldLabel")}
        for text, control in controls.items():
            with self.subTest(text=text):
                label = labels[text]
                self.assertIs(label.buddy(), control)
                interface = QAccessible.queryAccessibleInterface(control)
                self.assertTrue(any(
                    flag == QAccessible.RelationFlag.Label
                    for _target, flag in interface.relations()
                ))

    def test_normal_inheritance_has_no_permanent_recovery_button_in_settings(self):
        from src.ui.legacy_task_migration import LegacyTaskInheritanceController
        controller = LegacyTaskInheritanceController(app=self.root/'Lulu.app', product=self.root/'data',
            busy_probe=lambda: False)
        controller.state = 'complete'
        page = SettingsPage(self.cfg, inheritance_controller=controller)
        self.addCleanup(page.close)
        self.assertTrue(page.legacy_task_migration.isHidden())
        self.assertTrue(page.legacy_task_migration.button.isHidden())
        controller.state = 'failed'; controller.changed.emit()
        self.assertFalse(page.legacy_task_migration.isHidden())
        self.assertEqual(page.legacy_task_migration.button.text(), '重试资料检查')
        self.assertFalse(page.storage_operation_active())

    def test_storage_change_is_blocked_before_confirmation_when_other_work_is_active(self):
        page = self._page(external_storage_busy=lambda: True)
        with patch("src.ui.settings_page.dialogs.confirm") as confirm, \
                patch("src.ui.settings_page.dialogs.alert") as alert:
            page._start_storage_relocation(
                "models_dir",
                self.root / "models",
                self.root / "new-models",
            )
        confirm.assert_not_called()
        self.assertIn("正在进行", alert.call_args.args[2])

    def test_storage_activity_locks_every_setting_until_worker_finishes(self):
        page = self._page()
        emitted = []
        page.storage_activity_changed.connect(emitted.append)
        page._set_storage_activity(True)
        self.assertFalse(page.category_nav.isEnabled())
        self.assertFalse(page.category_stack.isEnabled())
        page._set_storage_activity(False)
        self.assertTrue(page.category_nav.isEnabled())
        self.assertTrue(page.category_stack.isEnabled())
        self.assertEqual(emitted, [True, False])

    def test_notification_policy_saves_only_after_native_permission_grant(self):
        page = self._page()
        changed = []
        page.preferences_changed.connect(changed.append)
        page.notification_select.set_value("always", emit=True)
        self.assertEqual(self.cfg.notification_policy, "never")
        self.assertFalse(page.notification_select.isEnabled())
        self.assertEqual(len(self.notifications.callbacks), 1)

        self.notifications.answer(
            granted=True,
            status=AuthorizationStatus.AUTHORIZED,
            reason="authorized",
        )
        self.app.processEvents()
        self.assertEqual(self.cfg.notification_policy, "always")
        self.assertTrue(page.notification_select.isEnabled())
        self.assertEqual(page.notification_select.currentData(), "always")
        self.assertIn("notification_policy", changed)

    def test_notification_denial_or_missing_bridge_restores_never(self):
        page = self._page()
        page.notification_select.set_value("inactive", emit=True)
        with patch("src.ui.settings_page.dialogs.alert") as alert:
            self.notifications.answer(
                granted=False,
                status=AuthorizationStatus.DENIED,
                reason="permission_denied",
            )
            self.app.processEvents()
        self.assertEqual(self.cfg.notification_policy, "never")
        self.assertEqual(page.notification_select.currentData(), "never")
        self.assertIn("没有允许", alert.call_args.args[2])

        page = self._page(notification_service=False)
        with patch("src.ui.settings_page.dialogs.alert") as alert:
            page.notification_select.set_value("always", emit=True)
        self.assertEqual(self.cfg.notification_policy, "never")
        self.assertEqual(page.notification_select.currentData(), "never")
        alert.assert_called_once()

    def test_folder_rows_show_consumer_hierarchy_and_complete_selectable_paths(self):
        page = self._page()
        self.assertEqual(tuple(page.path_labels), (
            "last_output_dir", "media_dir", "recording_dir", "tts_output_dir",
            "voice_library_dir", "models_dir",
        ))
        for field, label in page.path_labels.items():
            self.assertEqual(label.text(), str(Path(getattr(self.cfg, field)).expanduser()))
            self.assertTrue(label.wordWrap())
            self.assertTrue(label.textInteractionFlags() & Qt.TextSelectableByMouse)
            self.assertEqual(page.path_name_labels[field].text(), Path(getattr(self.cfg, field)).name)
            self.assertEqual(page.path_buttons[field].text(), "更改…")

        storage_copy = self._visible_copy(page.detail_scrolls["storage"])
        self.assertNotIn("不会移动已有文件", storage_copy)
        self.assertIn("安全迁移", storage_copy)

        old = self.cfg.media_dir
        with patch("src.ui.settings_page.QFileDialog.getExistingDirectory", return_value=""):
            page._pick_directory("media_dir")
        self.assertEqual(self.cfg.media_dir, old)

    def test_folder_save_complete_path_and_failure_rolls_back(self):
        page = self._page()
        selected = str(self.root / ("很长的下载媒体保存位置" * 6))
        with patch("src.ui.settings_page.QFileDialog.getExistingDirectory", return_value=selected):
            page._pick_directory("media_dir")
        self.assertEqual(page.path_labels["media_dir"].text(), selected)
        self.assertEqual(page.path_labels["media_dir"].toolTip(), selected)

        self.cfg.save_result = False
        old_recording = self.cfg.recording_dir
        with patch("src.ui.settings_page.dialogs.alert"):
            self.assertFalse(page._save_value("recording_dir", str(self.root / "new-recordings")))
        self.assertEqual(self.cfg.recording_dir, old_recording)

    def test_about_strict_brand_links_contact_and_product_boundary(self):
        page = self._page()
        self.assertEqual(page.about_tagline.text(), "一个专为你省钱的内容助手")
        from src.version import __version__

        self.assertIn(__version__, page.about_version.text())
        panel = page.update_panel
        self.assertEqual(panel.repository_url, "https://github.com/AidenXu-1/Lulu")
        self.assertEqual(panel.author_url, "https://github.com/AidenXu-1")
        self.assertEqual(panel.contact_button.text(), "查看微信二维码")
        self.assertEqual(panel.findChildren(QLabel, "authorContactPreview"), [])
        ordered_titles = []
        for index in range(panel.info_group.layout().count()):
            widget = panel.info_group.layout().itemAt(index).widget()
            if widget is None:
                continue
            labels = widget.findChildren(QLabel, "aboutInfoTitle")
            if labels:
                ordered_titles.append(labels[0].text())
        self.assertEqual(ordered_titles, [
            "GitHub 仓库", "作者 GitHub 主页", "作者联系方式",
        ])
        self.assertTrue(panel.software_group.isAncestorOf(panel.auto))
        self.assertTrue(panel.software_group.isAncestorOf(panel.primary))
        self.assertTrue(panel.boundary_group.isAncestorOf(panel.product_boundary))
        self.assertEqual(
            panel.product_boundary.text(),
            "Lulu坚持本地优先，本地模式下音频与逐字稿只留在本机，不会自动上传内容。\n"
            "只有主动使用云端模型或软件更新时，相关功能才会联网。",
        )
        self.assertIs(panel.auto_title.buddy(), panel.auto)

    def test_contact_button_opens_centered_modal_with_full_qr_only_after_click(self):
        page = self._page()
        created = []

        def fake_exec(dialog):
            created.append(dialog)
            return 0

        with patch.object(AuthorContactDialog, "exec", fake_exec):
            page.update_panel.contact_button.click()
        self.assertEqual(len(created), 1)
        dialog = created[0]
        self.assertFalse(dialog.qr_pixmap.isNull())
        qr_path = Path(T.asset("author_contact_wechat.jpg"))
        self.assertEqual(
            hashlib.sha256(qr_path.read_bytes()).hexdigest(),
            "20a0b9af3a2996b88f726d4b7e58d8824cf9c242b30ed54f337df3df22458e6b",
        )
        self.assertEqual(dialog.windowModality(), Qt.ApplicationModal)
        self.assertEqual(dialog.qr_label.alignment(), Qt.AlignCenter)

    def test_update_status_and_actions_reuse_shared_controller(self):
        page = self._page()
        panel = page.update_panel
        self.assertIs(panel.controller, self.controller)
        self.assertEqual(panel.state_title.text(), "尚未检查更新")
        self.assertEqual(panel.primary.text(), "检查更新")
        panel.primary.click()
        self.assertEqual(self.controller.check_calls, [True])

        self.controller.snapshot.ui_state = "up_to_date"
        self.controller.publish()
        self.assertEqual(panel.state_title.text(), "已是最新版本")

    def test_category_navigation_and_narrow_layout_have_no_horizontal_scroll(self):
        page = self._page()
        page.resize(620, 760)
        page.show()
        page.category_buttons["storage"].click()
        page.category_buttons["about"].setFocus()
        QTest.keyClick(page.category_buttons["about"], Qt.Key_Return)
        self.app.processEvents()
        self.assertEqual(page.current_category, "about")
        for scroll in page.detail_scrolls.values():
            self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        self.assertNotIn("appearance", page.detail_scrolls)
        page.close()

    def test_about_layout_preserves_reading_width_group_alignment_and_actions(self):
        page = self._page()
        page.show(); page.set_category("about")
        panel = page.update_panel
        scroll = page.detail_scrolls["about"]
        for width, height in ((968,760), (1248,940), (968,900), (968,760)):
            page.resize(width,height)
            baseline_right=None
            for state in ("up_to_date", "update_available", "installed"):
                with self.subTest(size=(width,height), state=state):
                    self.controller.snapshot.ui_state=state
                    self.controller.snapshot.latest_version="0.2.5"
                    self.controller.publish()
                    for _ in range(8): self.app.processEvents()
                    self.assertEqual(scroll.verticalScrollBar().maximum(), 0)
                    self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                    self.assertLessEqual(panel.width(),740)
                    right=panel.primary.mapTo(page,panel.primary.rect().topRight()).x()
                    if baseline_right is None: baseline_right=right
                    self.assertEqual(right,baseline_right)
                    self.assertEqual(right,panel.repository_button.mapTo(page,panel.repository_button.rect().topRight()).x())
                    self.assertEqual(panel.software_group.x(),panel.info_group.x())
                    self.assertEqual(panel.info_group.x(),panel.boundary_group.x())
                    gap=panel.mapTo(page,panel.rect().topLeft()).x()-page.category_nav.mapTo(page,page.category_nav.rect().topRight()).x()
                    self.assertGreaterEqual(gap,24)
                    self.assertLessEqual(gap,26)
                    bottom=panel.product_boundary.mapTo(scroll.viewport(),panel.product_boundary.rect().bottomLeft()).y()
                    self.assertLess(bottom,scroll.viewport().height())
        page.shutdown_workers();page.close()

    def test_real_main_window_1200_by_760_embeds_complete_about_page_in_paper(self):
        """验收真实壳层可用高度，不能再用独立 SettingsPage 尺寸代替。"""

        from src.config import AppConfig
        from src.core import secrets
        from src.ui.main_window import MainWindow

        def settle_layout():
            for _ in range(8):
                self.app.processEvents()

        old_backend = secrets._backend
        try:
            secrets._backend = lambda: None  # type: ignore[assignment]
            for theme_name in ("warm",):
                with self.subTest(theme=theme_name):
                    cfg = AppConfig()
                    cfg.theme = theme_name
                    for name, value in vars(self.cfg).items():
                        setattr(cfg, name, value)
                    controller = _Controller()
                    controller.snapshot.ui_state = "up_to_date"
                    controller.snapshot.latest_version = "0.2.4"
                    controller.snapshot.verified_manifest = True

                    # Native shell and about controls are real. Hidden web pages
                    # are outside this probe; avoid starting Chromium/services.
                    with patch.object(MainWindow, "_build_workbench", lambda shell: QWidget(shell)), patch.object(
                        MainWindow, "_prepare_library"
                    ), patch.object(
                        MainWindow, "_prepare_voice_page"
                    ), patch.object(
                        AppConfig,
                        "load",
                        classmethod(lambda _cls, current=cfg: current),
                    ), patch.object(
                        AppConfig,
                        "save",
                        lambda _cfg: True,
                    ), patch(
                        "src.ui.update_ui.UpdateController",
                        side_effect=lambda **_kwargs: controller,
                    ), patch(
                        "src.ui.macos.native_safe_area_top",
                        # 与真实安全预览一致：原生标题栏已启用，但 Cocoa
                        # 暂未返回可信 safe-area 时必须按 0 失败关闭。
                        return_value=0,
                    ):
                        window = MainWindow(task_queue_store=None)
                        window.resize(1200, 760)
                        window.enable_titlebar_inset()
                        window.show()
                        window._open_update_settings()
                        window.settings_page.set_category("about")
                        controller.publish()
                        settle_layout()

                        page = window.settings_page
                        scroll = page.detail_scrolls["about"]
                        info_layout = page.update_panel.info_group.layout()
                        info_rows = [
                            info_layout.itemAt(index).widget()
                            for index in range(info_layout.count())
                            if info_layout.itemAt(index).widget() is not None
                            and info_layout.itemAt(index).widget().objectName() == "aboutInfoRow"
                        ]
                        logo = next(
                            label
                            for label in page.findChildren(QLabel)
                            if label.accessibleName() == "Lulu 图标"
                        )
                        self.assertEqual(window.size().toTuple(), (1200, 760))
                        self.assertEqual(window.stack.currentIndex(), 6)
                        self.assertEqual(page.current_category, "about")
                        self.assertEqual(len(info_rows), 3)
                        self.assertEqual(
                            [row.minimumHeight() for row in info_rows],
                            [58, 58, 58],
                        )
                        self.assertEqual(
                            [row.height() for row in info_rows],
                            [58, 58, 58],
                        )
                        self.assertEqual(logo.size().toTuple(), (56, 56))
                        self.assertEqual(logo.pixmap().size().toTuple(), (56, 56))
                        self.assertEqual(page.update_panel.parentWidget().layout().spacing(), 12)
                        self.assertLessEqual(
                            page.update_panel.mapTo(
                                scroll.viewport(), page.update_panel.rect().topLeft()
                            ).y(),
                            120,
                            f"{theme_name} brand group remains too tall",
                        )
                        self.assertEqual(
                            scroll.verticalScrollBar().maximum(),
                            0,
                            f"{theme_name} real MainWindow still scrolls",
                        )
                        self.assertFalse(scroll.verticalScrollBar().isVisible())
                        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                        self.assertFalse(scroll.horizontalScrollBar().isVisible())
                        note_bottom = page.update_panel.product_boundary.mapTo(
                            scroll.viewport(), page.update_panel.product_boundary.rect().bottomLeft(),
                        ).y()
                        self.assertGreaterEqual(scroll.viewport().height()-note_bottom,16,
                            "product note is clipped by the real shell")
                        window.close()
                        settle_layout()
        finally:
            secrets._backend = old_backend  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
