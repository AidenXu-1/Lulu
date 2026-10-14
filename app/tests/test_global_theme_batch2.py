"""Batch 2 coreWorkspace theme isolation and page opt-in checks."""

from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFrame

from src.ui import theme as T
from src.ui.control_foundation import adopt_consumer_theme_scope


UI_DIR = Path(__file__).resolve().parents[1] / "src" / "ui"
CORE_PAGE_FILES = {
    "voice_page.py",
    "collection_page.py",
}
CORE_TRANSIENT_FILES = {"collection_history_dialog.py"}
VOICE_WORKBENCH_FILES = {"indextts25_workbench.py"}
AMENDED_LIBRARY_FILES = {"library_page.py"}
AMENDED_SETTINGS_FILES = {
    "models_page.py",
    "feishu_dialog.py",
    "settings_page.py",
    "license_device_page.py",
}
THEME_INFRA_FILES = {
    "theme.py",
    "control_foundation.py",
    "consumer_select.py",
    "consumer_date.py",
}


class GlobalThemeBatch2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_scope_is_explicit_and_does_not_reach_frozen_pages(self):
        root = QFrame()
        adopt_consumer_theme_scope(root, "coreWorkspace")
        self.assertEqual(root.property("consumerThemeScope"), "coreWorkspace")

        occurrences = set()
        for source in UI_DIR.glob("*.py"):
            if '"coreWorkspace"' in source.read_text(encoding="utf-8"):
                occurrences.add(source.name)
        self.assertEqual(
            occurrences,
            CORE_PAGE_FILES
            | CORE_TRANSIENT_FILES
            | VOICE_WORKBENCH_FILES
            | AMENDED_LIBRARY_FILES
            | AMENDED_SETTINGS_FILES
            | THEME_INFRA_FILES,
        )
        self.assertNotIn("coreWorkspace", (UI_DIR / "main_window.py").read_text(encoding="utf-8"))
        self.assertFalse((UI_DIR / "transcript_panel.py").exists())

    def test_active_page_roots_explicitly_adopt_the_same_scope(self):
        for filename in CORE_PAGE_FILES:
            with self.subTest(filename=filename):
                source = (UI_DIR / filename).read_text(encoding="utf-8")
                self.assertIn('adopt_consumer_theme_scope(self, "coreWorkspace")', source)

    def test_active_pages_use_the_same_editorial_header_and_paper_canvas(self):
        expected_markers = {
            "voice_page.py": "voice",
            "collection_page.py": "collection",
        }
        for filename in CORE_PAGE_FILES:
            with self.subTest(filename=filename):
                source = (UI_DIR / filename).read_text(encoding="utf-8")
                self.assertIn("build_consumer_page_header(", source)
                self.assertIn("PaperTextureFrame(", source)
                self.assertIn(
                    f'self.setProperty("workspacePage", "{expected_markers[filename]}")',
                    source,
                )


    def test_batch2_page_identity_never_restyles_the_transcription_master_shell(self):
        for theme_name, palette in T.THEMES.items():
            with self.subTest(theme=theme_name):
                consumer = T.consumer_theme(theme_name)
                shell_qss = T.build_consumer_global_shell_qss(consumer, palette)
                workspace_qss = T.build_consumer_core_workspace_qss(consumer)
                self.assertIn('QFrame#sidebar[consumerThemeScope="globalShell"]', shell_qss)
                self.assertIn('QFrame#titlebarPaperCenter[consumerThemeScope="globalShell"]', shell_qss)
                self.assertNotIn("rightPanelWrap", shell_qss)
                self.assertNotIn("titlebarPaperRight", shell_qss)
                self.assertNotIn("rightPanelWrap", workspace_qss)
                self.assertNotIn("titlebarPaperRight", workspace_qss)
                for marker in ("voice", "collection"):
                    self.assertIn(f'workspacePage="{marker}"', workspace_qss)
                for marker in ("copywriting", "contentOutput"):
                    self.assertNotIn(f'workspacePage="{marker}"', workspace_qss)

    def test_paper_build_scoped_workspace_surfaces_and_controls(self):
        for theme_name, palette in T.THEMES.items():
            with self.subTest(theme=theme_name):
                consumer = T.consumer_theme(theme_name)
                workspace_qss = T.build_consumer_core_workspace_qss(consumer)
                full_qss = T.build_qss(palette)
                self.assertIn(
                    'QWidget#root[consumerThemeScope="coreWorkspace"]',
                    workspace_qss,
                )
                self.assertIn(consumer.canvas, workspace_qss)
                self.assertIn(consumer.surface_focal, workspace_qss)
                self.assertIn(consumer.surface_work, workspace_qss)
                self.assertIn(consumer.ink, workspace_qss)
                self.assertIn(consumer.accent, workspace_qss)
                self.assertIn("QFrame#studioTitleRule", workspace_qss)
                self.assertIn('surfaceVariant="coreWorkbench"', workspace_qss)
                self.assertIn('surfaceVariant="uploadHero"', workspace_qss)
                self.assertIn('surfaceVariant="tableWorkspace"', workspace_qss)
                self.assertIn('QMenu[consumerThemeScope="coreWorkspace"]', workspace_qss)
                self.assertIn('QDialog[consumerThemeScope="coreWorkspace"]', workspace_qss)
                self.assertIn(workspace_qss, full_qss)
                self.assertNotIn('consumerThemeScope="globalShell"', workspace_qss)
                self.assertNotIn('consumerThemeScope="transcriptionWorkbench"', workspace_qss)

    def test_task_more_menu_keeps_transcription_workbench_theme_scope(self):
        source = (UI_DIR / "main_window.py").read_text(encoding="utf-8")
        self.assertIn(
            'm.setProperty("consumerThemeScope", "transcriptionWorkbench")',
            source,
        )
        for label in ("原链接", "存入 Obsidian", "存入飞书"):
            self.assertIn(f'm.addAction("{label}"', source)

    def test_consumer_custom_paint_colors_preserve_css_alpha(self):
        translucent = T.consumer_qcolor("rgba(83,125,150,0.08)")
        opaque = T.consumer_qcolor("#537D96")

        self.assertTrue(translucent.isValid())
        self.assertEqual(translucent.getRgb()[:3], (83, 125, 150))
        self.assertAlmostEqual(translucent.alphaF(), 0.08, places=2)
        self.assertEqual(opaque.name().upper(), "#537D96")


    def test_core_workspace_consumer_select_callsites_stay_authorized(self):
        callsites = []
        for source in UI_DIR.glob("*.py"):
            text = source.read_text(encoding="utf-8")
            if "ConsumerSelect(" in text and 'consumer_theme_scope="coreWorkspace"' in text:
                callsites.append(source.name)
        self.assertCountEqual(
            callsites,
            [
                "collection_history_dialog.py",
                "settings_page.py",
                "indextts25_workbench.py",
                "collection_page.py",
                "voice_page.py",
            ],
        )
        voice_source = (UI_DIR / "voice_page.py").read_text(encoding="utf-8")
        self.assertIn('consumer_theme_name=T.ACTIVE.name', voice_source)

    def test_u2_collection_selects_all_use_clipped_paper_popups(self):
        opt_in_files = []
        for source in UI_DIR.glob("*.py"):
            text = source.read_text(encoding="utf-8")
            if "paper_popup=True" in text:
                opt_in_files.append(source.name)
        self.assertCountEqual(
            opt_in_files,
            ["collection_history_dialog.py", "collection_page.py", "indextts25_workbench.py",
             "qwen3_workbench.py", "voxcpm2_workbench.py"],
        )
        collection_source = (UI_DIR / "collection_page.py").read_text(encoding="utf-8")
        history_source = (UI_DIR / "collection_history_dialog.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(collection_source.count("ConsumerSelect("), 6)
        self.assertEqual(collection_source.count("paper_popup=True"), 6)
        self.assertEqual(collection_source.count("clip_popup_corners=True"), 6)
        self.assertEqual(collection_source.count("ConsumerActionPopup("), 1)
        self.assertIn(
            "from .consumer_select import ConsumerActionPopup, ConsumerSelect",
            collection_source,
        )
        self.assertIn(
            "self.data_location_select.row_activated.connect(self._open_data_location)",
            collection_source,
        )
        self.assertIn(
            "self.data_location_select.button_activated.connect(self._choose_data_location)",
            collection_source,
        )
        self.assertEqual(history_source.count("ConsumerSelect("), 1)
        self.assertEqual(history_source.count("paper_popup=True"), 1)
        self.assertEqual(history_source.count("clip_popup_corners=True"), 1)

    def test_voice_source_picker_uses_the_shared_consumer_paper_contract(self):
        voice_source = (UI_DIR / "voice_page.py").read_text(encoding="utf-8")
        shared_source = (UI_DIR / "document_source_controls.py").read_text(encoding="utf-8")
        theme_source = (UI_DIR / "theme.py").read_text(encoding="utf-8")

        self.assertIn("empty = _PaperEnvelopePicker(self._consumer, wrap)", voice_source)
        self.assertIn("empty.setFixedHeight(canvas_height)", voice_source)
        self.assertNotIn("empty = _EnvelopeTrigger(wrap)", voice_source)
        self.assertIn('workspacePage="voice"', theme_source)

        shared_tree = ast.parse(shared_source)
        envelope = next(
            node
            for node in shared_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "PaperEnvelopePicker"
        )
        envelope_paint = next(
            ast.get_source_segment(shared_source, item) or ""
            for item in envelope.body
            if isinstance(item, ast.FunctionDef) and item.name == "paintEvent"
        )
        self.assertIn("_draw_natural_paper_grain", envelope_paint)
        self.assertIn("_draw_cord", envelope_paint)
        self.assertIn("consumer.accent_hairline_active", envelope_paint)

    def test_voice_primary_workbench_uses_the_approved_compact_controls(self):
        source = (UI_DIR / "voice_page.py").read_text(encoding="utf-8")
        self.assertIn('self._indextts2_emotion_mode = "follow"', source)
        self.assertIn('header_tools.setObjectName("voiceHeaderTools")', source)
        self.assertIn('header_tools_layout.addWidget(self._build_engine_switch()', source)
        self.assertNotIn('root.addWidget(self._build_engine_switch())', source)
        self.assertIn('seg.setProperty("sourceTabsVisual", "consumerSegment")', source)
        self.assertIn('adopt_segment(seg, source_buttons', source)
        self.assertIn('empty = _PaperEnvelopePicker(self._consumer, wrap)', source)
        self.assertIn('empty.setFixedHeight(canvas_height)', source)
        self.assertIn('empty_meta = QLabel("从文稿库中选择内容进行配音")', source)
        self.assertIn('VOICE_SOURCE_CANVAS_HEIGHT = 142', source)
        self.assertIn('VOICE_SOURCE_INNER_BOTTOM_GAP = 10', source)
        self.assertIn('source_canvas_layout.setContentsMargins(0, 0, 0, VOICE_SOURCE_INNER_BOTTOM_GAP)', source)
        self.assertIn('self.indextts2_status_banner_host.setVisible(False)', source)
        self.assertIn('page = _GardenFollowPanel()', source)
        self.assertIn('note = QLabel("♪", page)', source)
        self.assertIn('"顺其声，自然成韵"', source)
        self.assertIn('self.setFixedSize(176 if not compact else 112', source)
        self.assertIn('empty_layout.setContentsMargins(28, 14, 28, 14)', source)
        self.assertIn('empty_layout.addLayout(empty_copy, 1)', source)
        self.assertIn('drop = _PaperBoxFileDropArea(', source)
        self.assertIn('title="拖入情绪参考音频"', source)
        self.assertIn('self.indextts2_weight_title = QLabel("整体情感权重")', source)
        self.assertIn('self.indextts2_weight_slider.setRange(30, 110)', source)
        self.assertIn('accessible_name="IndexTTS 2.5整体情感权重"', source)


if __name__ == "__main__":
    unittest.main()
