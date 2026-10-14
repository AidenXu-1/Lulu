"""Theme isolation for models, Feishu settings and the settings centre."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QWidget,
)

from src.ui import feishu_dialog as feishu_dialog_module
from src.ui import models_page as models_page_module
from src.ui import settings_page as settings_page_module
from src.ui import theme as T
from src.ui.control_foundation import PaperTextureFrame


class _ModelsCfg:
    engine_name = "local_sensevoice"
    copywriting_cloud_provider = "agnes"

    def save(self):
        return True


class _FeishuCfg:
    feishu_target = {}
    feishu_creation_target = {}
    feishu_creation_presets = []
    feishu_table_targets = []
    feishu_storage_preferences = {}


class _SettingsCfg:
    def __init__(self, root: Path):
        self.theme = T.ACTIVE.name
        self.last_output_dir = str(root / "exports")
        self.media_dir = str(root / "media")
        self.recording_dir = str(root / "recordings")
        self.tts_output_dir = str(root / "voices")
        self.voice_library_dir = str(root / "voice-library")
        self.models_dir = str(root / "models")
        self.auto_update_enabled = False
        self.notification_policy = "never"
        self.notify_on_failure = True
        self.notification_sound_enabled = True
        self.prevent_sleep_during_tasks = False
        self.confirm_exit_during_tasks = True
        self.completion_action = "stay"
        self.feishu_target = {}
        self.feishu_creation_target = {}
        self.feishu_creation_presets = []
        self.feishu_table_targets = []
        self.feishu_storage_preferences = {}

    def save(self):
        return True


class GlobalThemeBatch3SettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.old_theme = T.ACTIVE.name
        self.old_qss = self.app.styleSheet()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._model_patchers = (
            patch("src.core.secrets.configuration_loaded", return_value=False),
            patch.object(models_page_module.ModelsPage, "refresh", lambda _self: None),
        )
        for patcher in self._model_patchers:
            patcher.start()

    def tearDown(self):
        T.apply_theme(self.old_theme)
        self.app.setStyleSheet(self.old_qss)
        for patcher in reversed(self._model_patchers):
            patcher.stop()
        self.tmp.cleanup()

    def _prepare_theme(self, name: str) -> None:
        T.apply_theme(name)
        self.app.setStyleSheet(T.QSS)

    def _models_page(self, theme_name: str, size: tuple[int, int]):
        self._prepare_theme(theme_name)
        page = models_page_module.ModelsPage(_ModelsCfg())
        page.resize(*size)
        page.show()
        self.app.processEvents()
        self.addCleanup(page.close)
        return page

    def _settings_page(self, theme_name: str, size: tuple[int, int]):
        self._prepare_theme(theme_name)
        cfg = _SettingsCfg(self.root)
        page = settings_page_module.SettingsPage(cfg)
        page.resize(*size)
        page.show()
        self.app.processEvents()
        self.addCleanup(page.close)
        return page

    def _feishu_dialog(self, theme_name: str, size: tuple[int, int]):
        self._prepare_theme(theme_name)
        with patch.object(feishu_dialog_module.secrets, "get_config", return_value={}):
            dialog = feishu_dialog_module.FeishuConfigDialog(None, _FeishuCfg())
        dialog.resize(*size)
        dialog.show()
        self.app.processEvents()
        self.addCleanup(dialog.close)
        return dialog

    def test_three_surfaces_use_explicit_paper_scope_and_complete_headers(self):
        models = self._models_page("warm", (900, 700))
        settings = self._settings_page("warm", (900, 700))
        feishu = self._feishu_dialog("warm", (620, 760))

        self.assertEqual(models.property("consumerThemeScope"), "coreWorkspace")
        self.assertEqual(models.property("workspacePage"), "models")
        self.assertFalse(hasattr(models, "title_label"))
        self.assertTrue(models.view.url().toLocalFile().endswith("model_settings/assets/index.html"))

        self.assertEqual(settings.property("consumerThemeScope"), "coreWorkspace")
        self.assertEqual(settings.property("workspacePage"), "settings")
        self.assertEqual(settings.title_label.text(), "设置")
        self.assertIsNotNone(settings.findChild(QFrame, "studioTitleRule"))
        self.assertIsInstance(settings.findChild(QFrame, "coreCanvas"), PaperTextureFrame)
        self.assertIsInstance(settings.findChild(QFrame, "settingsWorkbench"), PaperTextureFrame)

        self.assertEqual(feishu.windowTitle(), '连接飞书')
        self.assertEqual(feishu.panel.objectName(), 'feishuConnection')
        self.assertFalse(hasattr(feishu, 'space_field'))

    def test_settings_feishu_category_only_exposes_connection(self):
        page=self._settings_page('warm',(900,700));page.set_category('feishu')
        panel=page.feishu_connection
        texts=[b.text() for b in panel.findChildren(QPushButton)]
        self.assertIn('连接飞书',texts)
        self.assertFalse(set(texts)&{'管理预设','添加已有表格','新建作品库'})
        self.assertFalse(hasattr(page,'_manage_feishu_presets'))

    def test_settings_about_uses_current_mascot_asset(self):
        page = self._settings_page("warm", (900, 700))
        logos = [
            label
            for label in page.findChildren(QLabel)
            if label.property("brandAsset")
        ]
        self.assertEqual(len(logos), 1)
        self.assertEqual(
            logos[0].property("brandAsset"),
            "logo_mascot_transparent.png",
        )
        self.assertFalse(logos[0].pixmap().isNull())

    def test_opening_connection_preserves_legacy_destinations(self):
        cfg=_SettingsCfg(self.root);cfg.feishu_creation_presets=[{'name':'原位置'}];cfg.feishu_table_targets=[{'name':'原表格'}]
        with patch('src.core.secrets.get_config') as read:
            page=settings_page_module.SettingsPage(cfg);page.set_category('feishu');self.app.processEvents()
            read.assert_not_called()
        self.assertEqual(cfg.feishu_table_targets,[{'name':'原表格'}]);self.assertEqual(cfg.feishu_creation_presets,[{'name':'原位置'}])
        page.shutdown_workers();page.close()



    def test_model_purpose_pages_do_not_inherit_the_root_canvas_background(self):
        models = self._models_page("warm", (900, 700))

        self.assertFalse(hasattr(models, "purpose_stack"))
        self.assertEqual(models.view.geometry(), models.contentsRect())
        self.assertEqual(models.findChildren(QWidget, "root"), [])

    def test_connection_setup_scrolls_at_small_sizes(self):
        for width in (520,640,900):
            d=self._feishu_dialog('warm',(width,520));d.panel.details.show();self.app.processEvents()
            scroll=d.panel.scroll
            self.assertEqual(scroll.horizontalScrollBar().maximum(),0)
            self.assertGreater(scroll.verticalScrollBar().maximum(),0)
            for b in d.panel.details.findChildren(QPushButton):
                self.assertTrue(b.parentWidget().rect().contains(b.geometry()))


    def test_paper_and_two_sizes_keep_workspaces_inside_visible_bounds(self):
        for theme_name in T.THEMES:
            for size in ((900, 700), (680, 560)):
                with self.subTest(surface="models", theme=theme_name, size=size):
                    page = self._models_page(theme_name, size)
                    self.assertEqual(page.view.geometry(), page.contentsRect())
                    self.assertEqual(page.layout().contentsMargins().left(), 0)

                with self.subTest(surface="settings", theme=theme_name, size=size):
                    page = self._settings_page(theme_name, size)
                    canvas = page.findChild(PaperTextureFrame, "coreCanvas")
                    workbench = page.findChild(PaperTextureFrame, "settingsWorkbench")
                    self.assertLessEqual(workbench.geometry().bottom(), canvas.contentsRect().bottom())
                    self.assertEqual(page.category_stack.currentIndex(), 0)

            for size in ((620, 760), (520, 680)):
                with self.subTest(surface="feishu", theme=theme_name, size=size):
                    dialog = self._feishu_dialog(theme_name, size)
                    self.assertGreaterEqual(dialog.panel.width(), 320)
                    self.assertLessEqual(dialog.panel.geometry().bottom(),dialog.contentsRect().bottom())
                    self.assertEqual(dialog.panel.scroll.horizontalScrollBarPolicy(),Qt.ScrollBarAlwaysOff)

    def test_existing_business_controls_and_connections_are_preserved(self):
        models_source = Path(models_page_module.__file__).read_text(encoding="utf-8")
        feishu_source = Path(feishu_dialog_module.__file__).read_text(encoding="utf-8")
        settings_source = Path(settings_page_module.__file__).read_text(encoding="utf-8")

        # Model lifecycle and browser interactions are exercised in the model-settings tests.
        self.assertIn("from .model_settings.widget import ModelsPage", models_source)
        for marker in (
            "feishu.exchange_code(holder[\"code\"], ru)",
            "feishu.export_collection_to_new_bitable(",
            "record_from_transcript(",
            "self.panel = FeishuConnectionPanel(cfg, self)",
        ):
            self.assertIn(marker, feishu_source)
        for marker in (
            "self.failure_notification_switch.toggled.connect",
            "self.completion_action_select.value_changed.connect",
            "models_manager.relocate_models(",
            "self.update_controller",
        ):
            self.assertIn(marker, settings_source)

    def test_page_local_qss_does_not_target_frozen_sample_or_global_shell(self):
        for module, builder_name in (
            (settings_page_module, "_settings_workspace_qss"),
            (feishu_dialog_module, "_feishu_dialog_qss"),
        ):
            qss = getattr(module, builder_name)(T.consumer_theme("warm"))
            self.assertIn('consumerThemeScope="coreWorkspace"', qss)
            self.assertNotIn("transcriptionWorkbench", qss)
            self.assertNotIn("globalShell", qss)


if __name__ == "__main__":
    unittest.main()
