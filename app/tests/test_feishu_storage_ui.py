"""飞书多维表格存储配置：纯本地迁移、规范化与 Obsidian 隔离。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config as config_module  # noqa: E402
from src.config import (  # noqa: E402
    AppConfig,
    FEISHU_DRIVE_ROOT_PRESET_ID,
    FEISHU_STORAGE_CONTRACT_VERSION,
)
from src.core import feishu  # noqa: E402
from src.core.collect import export as collection_export  # noqa: E402
from src.core.feishu_storage import WorkArchiveRecord  # noqa: E402
from src.ui import feishu_dialog  # noqa: E402


class TestFeishuActionDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self) -> feishu_dialog.FeishuActionDialog:
        return feishu_dialog.FeishuActionDialog(
            None,
            "存入飞书作品库",
            "选择这次数据的去向。默认设置可在“设置－飞书知识库”中调整。",
            [
                feishu_dialog.FeishuActionOption(
                    "使用默认表格",
                    "暂未设置默认表格",
                    icon="link",
                    badge="未设置",
                    group="已有表格",
                ),
                feishu_dialog.FeishuActionOption(
                    "临时输入已有表格",
                    "粘贴一张已有多维表格链接，仅对本次保存生效",
                    icon="plus",
                    badge="仅本次",
                    group="已有表格",
                ),
                feishu_dialog.FeishuActionOption(
                    "使用默认位置",
                    "云盘（我的文稿库）",
                    icon="layers",
                    badge="默认",
                    group="新建表格",
                    tone="recommended",
                ),
                feishu_dialog.FeishuActionOption(
                    "临时选择新建位置",
                    "选择云盘或知识库层级，仅对本次保存生效",
                    icon="folder",
                    badge="仅本次",
                    group="新建表格",
                ),
            ],
        )

    def test_four_routes_are_grouped_cards_with_consumer_interaction_states(self):
        dialog = self._dialog()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        self.assertEqual(dialog.objectName(), "feishuActionDialog")
        self.assertGreaterEqual(dialog.width(), 570)
        self.assertEqual(len(dialog.option_cards), 4)
        self.assertEqual(
            [card.property("optionIndex") for card in dialog.option_cards],
            [0, 1, 2, 3],
        )
        self.assertEqual(
            [
                label.text()
                for label in dialog.findChildren(QLabel, "feishuActionGroupTitle")
            ],
            ["已有表格", "新建表格"],
        )
        self.assertEqual(
            [
                label.text()
                for label in dialog.findChildren(QLabel, "feishuActionBadge")
            ],
            ["未设置", "仅本次", "默认", "仅本次"],
        )
        for card in dialog.option_cards:
            self.assertGreaterEqual(card.height(), 74)
            self.assertEqual(card.cursor().shape(), Qt.PointingHandCursor)
            self.assertEqual(card.focusPolicy(), Qt.StrongFocus)
            self.assertGreater(card.width(), 480)

    def test_card_click_returns_original_business_index(self):
        dialog = self._dialog()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        dialog.option_cards[2].click()

        self.assertEqual(dialog.result(), 3)

    def test_flow_page_switch_keeps_one_native_window_and_returns_in_place(self):
        dialog = self._dialog()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()
        native_window = int(dialog.winId())

        def root_handler(_index, _action):
            dialog.push_flow_page(
                "选择知识库层级",
                "在当前窗口继续完成设置。",
                [
                    feishu_dialog.FeishuActionOption(
                        "知识库最高层级",
                        "直接放在知识库顶层",
                        icon="layers",
                    )
                ],
                lambda _picked, _inline: None,
            )

        dialog.set_flow_handler(root_handler)
        dialog.option_cards[0].click()
        self.app.processEvents()

        self.assertTrue(dialog.isVisible())
        self.assertEqual(int(dialog.winId()), native_window)
        self.assertEqual(dialog.windowTitle(), "选择知识库层级")
        back = dialog.findChild(QPushButton, "feishuDialogBack")
        self.assertTrue(back.isVisible())

        back.click()
        self.app.processEvents()

        self.assertTrue(dialog.isVisible())
        self.assertEqual(int(dialog.winId()), native_window)
        self.assertEqual(dialog.windowTitle(), "存入飞书作品库")

    def test_passive_saved_card_exposes_rename_and_delete_without_navigation(self):
        dialog = feishu_dialog.FeishuActionDialog(
            None,
            "管理已有表格预设",
            "直接在卡片上管理。",
            [
                feishu_dialog.FeishuActionOption(
                    "AI 口播对标库",
                    "已有多维表格",
                    icon="link",
                    actions=("rename", "delete"),
                    selectable=False,
                )
            ],
        )
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        card = dialog.option_cards[0]
        self.assertEqual(card.focusPolicy(), Qt.NoFocus)
        actions = card.findChildren(QPushButton, "feishuInlineAction")
        self.assertEqual([button.property("action") for button in actions], ["rename", "delete"])

        actions[0].click()

        self.assertEqual(dialog.result(), 1)
        self.assertEqual(dialog.selected_inline_action, "rename")

    def test_dialog_remains_uncropped_at_supported_widths(self):
        for width in (570, 640, 760):
            with self.subTest(width=width):
                dialog = self._dialog()
                dialog.resize(width, 680)
                dialog.show()
                self.app.processEvents()
                scroll = dialog.findChild(QScrollArea, "feishuActionScroll")
                self.assertIsNotNone(scroll)
                self.assertFalse(scroll.verticalScrollBar().isVisible())
                for card in dialog.option_cards:
                    self.assertGreaterEqual(card.width(), width - 100)
                    self.assertGreaterEqual(card.minimumHeight(), 62)
                    self.assertGreaterEqual(card.height(), card.minimumHeight())
                    for child in card.findChildren(QLabel):
                        self.assertTrue(
                            card.rect().adjusted(-1, -1, 1, 1).contains(child.geometry()),
                            (width, child.objectName(), child.geometry()),
                        )
                dialog.close()

    def test_saved_presets_keep_four_card_viewport_and_scroll_extra_items(self):
        dialog = feishu_dialog.FeishuActionDialog(
            None,
            "管理已有表格预设",
            "预设数量不改变窗口大小。",
            [
                feishu_dialog.FeishuActionOption(
                    "添加已有表格",
                    "长期预设",
                    group="快捷操作",
                ),
                *[
                    feishu_dialog.FeishuActionOption(
                        f"作品库 {index + 1}",
                        "已保存表格",
                        group="已保存表格",
                        selectable=False,
                    )
                    for index in range(6)
                ],
            ],
            cancel="关闭",
        )
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()
        scroll = dialog.findChild(QScrollArea, "feishuActionScroll")
        viewport = scroll.viewport()

        self.assertEqual(dialog.height(), 760)
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        fourth_saved_bottom = dialog.option_cards[4].mapTo(
            viewport,
            dialog.option_cards[4].rect().bottomLeft(),
        ).y()
        fifth_saved_top = dialog.option_cards[5].mapTo(
            viewport,
            dialog.option_cards[5].rect().topLeft(),
        ).y()
        self.assertLessEqual(fourth_saved_bottom, viewport.height())
        self.assertGreater(fifth_saved_top, viewport.height())


class FeishuStorageConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name) / "config" / "config.json"
        path_patch = patch.object(config_module, "_CONFIG_PATH", self.config_path)
        path_patch.start()
        self.addCleanup(path_patch.stop)

    def write_config(self, payload: dict) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )


class TestFeishuStorageMigration(FeishuStorageConfigCase):
    def test_legacy_target_migrates_locally_without_changing_obsidian(self):
        obsidian_dir = "/资料库/兆基 Obsidian/原样  "
        legacy = {
            "space_id": "SPACE1",
            "node_token": "NODE1",
            "name": "内容中心",
            "bitable_mode": "existing",
            "bitable_url": "https://example.feishu.cn/base/APP1?table=TBL1",
            "bitable_name": "AI 口播作品库",
        }
        self.write_config(
            {
                "theme": "dark",
                "obsidian_dir": obsidian_dir,
                "feishu_target": legacy,
            }
        )

        loaded = AppConfig.load()

        self.assertEqual(loaded.feishu_target, legacy)
        self.assertEqual(
            loaded.feishu_creation_target,
            {
                "destination_kind": "wiki",
                "space_id": "SPACE1",
                "node_token": "NODE1",
                "name": "内容中心",
            },
        )
        self.assertEqual(
            [item["destination_kind"] for item in loaded.feishu_creation_presets],
            ["drive", "wiki"],
        )
        self.assertEqual(len(loaded.feishu_table_targets), 1)
        migrated = loaded.feishu_table_targets[0]
        self.assertEqual(migrated["name"], "AI 口播作品库")
        self.assertEqual(migrated["url"], legacy["bitable_url"])
        self.assertEqual(migrated["status"], "pending_validation")
        self.assertEqual(migrated["source_kind"], "existing")
        self.assertTrue(migrated["target_id"].startswith("feishu-table-"))
        self.assertEqual(
            loaded.feishu_storage_preferences["recent_target_id"],
            migrated["target_id"],
        )
        self.assertEqual(
            loaded.feishu_storage_preferences["selection_target_id"],
            migrated["target_id"],
        )
        self.assertEqual(
            loaded.feishu_storage_preferences["existing_selection_target_id"],
            migrated["target_id"],
        )
        self.assertEqual(loaded.obsidian_dir.encode("utf-8"), obsidian_dir.encode("utf-8"))
        self.assertEqual(
            loaded.feishu_storage_contract_version,
            FEISHU_STORAGE_CONTRACT_VERSION,
        )

    def test_migration_is_idempotent_and_does_not_duplicate_pending_target(self):
        self.write_config(
            {
                "obsidian_dir": "/vault/Obsidian",
                "feishu_target": {
                    "space_id": "SPACE1",
                    "name": "知识库",
                    "bitable_url": "https://example.feishu.cn/wiki/WIKI1?table=TBL1",
                    "bitable_name": "长期作品库",
                },
            }
        )
        first = AppConfig.load()
        self.assertTrue(first.save())
        first_bytes = self.config_path.read_bytes()
        first_target = dict(first.feishu_table_targets[0])

        second = AppConfig.load()
        self.assertEqual(second.feishu_table_targets, [first_target])
        self.assertTrue(second.save())

        self.assertEqual(self.config_path.read_bytes(), first_bytes)
        self.assertEqual(len(AppConfig.load().feishu_table_targets), 1)

    def test_legacy_input_added_after_construction_is_imported_on_save(self):
        config = AppConfig(obsidian_dir="/vault/keep")
        config.feishu_target = {
            "space_id": "SPACE2",
            "node_token": "NODE2",
            "name": "新位置",
            "bitable_url": "https://example.feishu.cn/base/APP2?table=TBL2",
            "bitable_name": "后续新增目标",
        }

        self.assertTrue(config.save())
        loaded = AppConfig.load()

        self.assertEqual(loaded.feishu_creation_target["space_id"], "SPACE2")
        self.assertEqual(len(loaded.feishu_table_targets), 1)
        self.assertEqual(loaded.feishu_table_targets[0]["name"], "后续新增目标")
        self.assertEqual(loaded.obsidian_dir, "/vault/keep")


class TestFeishuStorageNormalization(FeishuStorageConfigCase):
    def test_concrete_tables_and_creation_destinations_keep_independent_defaults(self):
        config = AppConfig(
            obsidian_dir="/vault/保持原样",
            feishu_table_targets=[
                {
                    "target_id": "existing-ai",
                    "name": "已有 AI 表",
                    "url": "https://example.feishu.cn/base/EXIST?table=TABLE1",
                    "status": "ready",
                    "source_kind": "existing",
                },
                {
                    "target_id": "created-ai",
                    "name": "Lulu 新建 AI 表",
                    "url": "https://example.feishu.cn/base/CREATE?table=TABLE2",
                    "status": "ready",
                    "source_kind": "created",
                },
            ],
            feishu_creation_presets=[
                {
                    "preset_id": "wiki-ai",
                    "destination_kind": "wiki",
                    "space_id": "SPACE-AI",
                    "node_token": "NODE-AI",
                    "name": "AI 知识库 / 对标层级",
                }
            ],
            feishu_storage_preferences={
                "single_target_id": "created-ai",
                "selection_target_id": "existing-ai",
                "existing_single_target_id": "existing-ai",
                "existing_selection_target_id": "created-ai",
                "created_single_destination_id": "wiki-ai",
                "created_selection_destination_id": FEISHU_DRIVE_ROOT_PRESET_ID,
                "recent_target_id": "created-ai",
            },
        )

        self.assertEqual(
            [item["source_kind"] for item in config.feishu_table_targets],
            ["existing", "created"],
        )
        self.assertEqual(
            config.feishu_storage_preferences["existing_single_target_id"],
            "existing-ai",
        )
        self.assertEqual(
            config.feishu_storage_preferences["created_single_destination_id"],
            "wiki-ai",
        )
        self.assertEqual(
            config.feishu_storage_preferences["existing_selection_target_id"],
            "created-ai",
        )
        self.assertEqual(
            config.feishu_storage_preferences["created_selection_destination_id"],
            FEISHU_DRIVE_ROOT_PRESET_ID,
        )
        self.assertEqual(config.obsidian_dir, "/vault/保持原样")
        self.assertTrue(config.save())
        loaded = AppConfig.load()
        self.assertEqual(loaded.feishu_table_targets, config.feishu_table_targets)
        self.assertEqual(
            loaded.feishu_storage_preferences,
            config.feishu_storage_preferences,
        )

    def test_multiple_targets_round_trip_and_invalid_preferences_fail_closed(self):
        config = AppConfig(
            obsidian_dir="/vault/不触碰",
            feishu_creation_target={
                "space_id": "SPACE9",
                "node_token": "NODE9",
                "name": "竞品仓库",
            },
            feishu_table_targets=[
                {
                    "target_id": "ai",
                    "name": "AI 赛道",
                    "url": "https://example.feishu.cn/base/APPA?table=TBLA",
                    "app_token": "APPA",
                    "table_id": "TBLA",
                    "template_id": "lulu-work-library",
                    "schema_version": 1,
                    "field_map": {"title": {"field_id": "fld1", "type": 1}},
                    "status": "ready",
                },
                {
                    "target_id": "duplicate",
                    "name": "重复链接",
                    "url": "https://example.feishu.cn/base/APPA?table=TBLA",
                },
                {
                    "target_id": "beauty",
                    "name": "美妆赛道",
                    "url": "https://example.feishu.cn/base/APPB?table=TBLB",
                    "status": "ready",
                },
                {"target_id": "broken", "name": "无地址"},
            ],
            feishu_storage_preferences={
                "single_target_id": "ai",
                "selection_target_id": "beauty",
                "recent_target_id": "missing",
            },
        )

        self.assertEqual(
            [target["target_id"] for target in config.feishu_table_targets],
            ["ai", "beauty"],
        )
        self.assertEqual(config.feishu_storage_preferences["single_target_id"], "ai")
        self.assertEqual(
            config.feishu_storage_preferences["selection_target_id"], "beauty"
        )
        self.assertEqual(config.feishu_storage_preferences["recent_target_id"], "")
        self.assertTrue(config.save())

        loaded = AppConfig.load()
        self.assertEqual(loaded.feishu_creation_target, config.feishu_creation_target)
        self.assertEqual(loaded.feishu_table_targets, config.feishu_table_targets)
        self.assertEqual(
            loaded.feishu_storage_preferences,
            config.feishu_storage_preferences,
        )
        self.assertEqual(loaded.obsidian_dir, "/vault/不触碰")

    def test_new_creation_target_wins_over_stale_legacy_location(self):
        config = AppConfig(
            feishu_target={
                "space_id": "OLD",
                "node_token": "OLDNODE",
                "name": "旧位置",
            },
            feishu_creation_target={
                "space_id": "NEW",
                "node_token": "NEWNODE",
                "name": "新位置",
            },
        )

        self.assertEqual(config.feishu_creation_target["space_id"], "NEW")
        self.assertEqual(config.feishu_creation_target["node_token"], "NEWNODE")

    def test_malformed_nested_values_do_not_damage_unrelated_preferences(self):
        obsidian_dir = "/vault/原样保留  "
        self.write_config(
            {
                "theme": "light",
                "obsidian_dir": obsidian_dir,
                "feishu_target": ["invalid"],
                "feishu_creation_target": "invalid",
                "feishu_table_targets": {"invalid": True},
                "feishu_storage_preferences": ["invalid"],
                "feishu_storage_contract_version": "invalid",
            }
        )

        loaded = AppConfig.load()

        self.assertEqual(loaded.theme, "warm")
        self.assertEqual(loaded.obsidian_dir.encode("utf-8"), obsidian_dir.encode("utf-8"))
        self.assertEqual(loaded.feishu_target, {})
        self.assertEqual(loaded.feishu_creation_target, {})
        self.assertEqual(loaded.feishu_table_targets, [])
        self.assertEqual(
            loaded.feishu_storage_preferences,
            {
                "single_target_id": "",
                "selection_target_id": "",
                "existing_single_target_id": "",
                "existing_selection_target_id": "",
                "created_single_destination_id": "",
                "created_selection_destination_id": "",
                "recent_target_id": "",
            },
        )
        self.assertEqual(
            loaded.feishu_creation_presets,
            [
                {
                    "preset_id": FEISHU_DRIVE_ROOT_PRESET_ID,
                    "destination_kind": "drive",
                    "name": "云盘（我的文稿库）",
                }
            ],
        )

    def test_save_reload_preserves_obsidian_dir_exact_utf8_bytes(self):
        obsidian_dir = "/Users/兆基/大脑/Obsidian Vault  "
        config = AppConfig(
            obsidian_dir=obsidian_dir,
            feishu_creation_target={"space_id": "SPACE1", "name": "飞书位置"},
        )

        self.assertTrue(config.save())
        loaded = AppConfig.load()

        self.assertEqual(loaded.obsidian_dir.encode("utf-8"), obsidian_dir.encode("utf-8"))
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["obsidian_dir"].encode("utf-8"), obsidian_dir.encode("utf-8"))


class _MemoryConfig:
    def __init__(self, *, save_result: bool = True):
        self.obsidian_dir = "/vault/保持原样  "
        self.feishu_target = {}
        self.feishu_creation_target = {
            "destination_kind": "wiki",
            "space_id": "SPACE1",
            "node_token": "NODE1",
            "name": "内容仓库",
        }
        self.feishu_creation_presets = [
            {
                "preset_id": FEISHU_DRIVE_ROOT_PRESET_ID,
                "destination_kind": "drive",
                "name": "云盘（我的文稿库）",
            }
        ]
        self.feishu_table_targets = []
        self.feishu_storage_preferences = {}
        self.save_result = save_result
        self.save_calls = 0

    def save(self) -> bool:
        self.save_calls += 1
        return self.save_result


class TestFeishuStorageRouting(unittest.TestCase):
    def test_new_table_defaults_route_to_drive_or_selected_wiki_level(self):
        config = _MemoryConfig()
        config.feishu_creation_presets.append(
            {
                "preset_id": "wiki-level",
                "destination_kind": "wiki",
                "space_id": "SPACE2",
                "node_token": "NODE2",
                "name": "内容库 / 对标层级",
            }
        )
        config.feishu_storage_preferences = {
            "created_single_destination_id": "wiki-level",
            "created_selection_destination_id": FEISHU_DRIVE_ROOT_PRESET_ID,
        }

        single = feishu_dialog.preferred_creation_destination(
            config,
            context="single",
        )
        batch = feishu_dialog.preferred_creation_destination(
            config,
            context="batch_new",
        )

        self.assertEqual(single["destination_kind"], "wiki")
        self.assertEqual(single["space_id"], "SPACE2")
        self.assertEqual(single["node_token"], "NODE2")
        self.assertEqual(batch["destination_kind"], "drive")
        self.assertNotIn("space_id", batch)

    def test_standard_existing_worker_skips_cloud_duplicates_before_write(self):
        records = (
            WorkArchiveRecord(
                title="已经收录",
                platform="douyin",
                work_id="1",
                work_url="https://www.douyin.com/video/1",
            ),
            WorkArchiveRecord(
                title="本次新增",
                platform="douyin",
                work_id="2",
                work_url="https://www.douyin.com/video/2",
            ),
        )
        target = feishu.ExistingBitableTarget(
            "APP1",
            "TBL1",
            "长期作品库",
            "https://example.feishu.cn/base/APP1?table=TBL1",
        )
        receipt = feishu.BitableExportReceipt(
            url=target.url,
            row_total=1,
            row_success=1,
            row_failed=0,
            row_unconfirmed=0,
            row_cancelled=0,
            cover_candidate=0,
            cover_success=0,
            cover_skipped=0,
            cover_failed=0,
            cover_uncertain=0,
            cancelled=False,
            destination_app_token="APP1",
            destination_table_id="TBL1",
            destination_table_name="长期作品库",
        )
        worker = feishu_dialog.FeishuStorageWorker(
            records,
            {"mode": "existing", "url": target.url, "schema_version": 1},
            "不会新建",
        )
        outcomes = []
        worker.done.connect(lambda ok, payload: outcomes.append((ok, payload)))

        with (
            patch.object(
                feishu_dialog.feishu,
                "resolve_existing_bitable",
                return_value=target,
            ),
            patch.object(
                feishu_dialog.feishu,
                "find_existing_bitable_text_values",
                return_value={"https://www.douyin.com/video/1"},
            ) as find_existing,
            patch.object(
                feishu_dialog,
                "_archive_cover_preflight",
                return_value=((), ()),
            ) as cover_preflight,
            patch.object(
                feishu_dialog.feishu,
                "export_collection_to_new_bitable",
                return_value=receipt,
            ) as write,
        ):
            worker.run()

        self.assertTrue(outcomes[0][0])
        self.assertEqual(outcomes[0][1].row_duplicate_skipped, 1)
        find_existing.assert_called_once()
        self.assertEqual(find_existing.call_args.args[2], "原作品链接")
        self.assertNotEqual(find_existing.call_args.args[2], "Lulu作品键")
        self.assertEqual(cover_preflight.call_args.args[0], (records[1],))
        payloads = write.call_args.args[5]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["fields"]["标题"], "本次新增")

    def test_standard_existing_worker_search_failure_is_zero_write(self):
        record = WorkArchiveRecord(
            title="待查重",
            platform="douyin",
            work_id="1",
            work_url="https://www.douyin.com/video/1",
        )
        target = feishu.ExistingBitableTarget(
            "APP1",
            "TBL1",
            "长期作品库",
            "https://example.feishu.cn/base/APP1?table=TBL1",
        )
        worker = feishu_dialog.FeishuStorageWorker(
            (record,),
            {"mode": "existing", "url": target.url, "schema_version": 1},
            "不会新建",
        )
        outcomes = []
        worker.done.connect(lambda ok, payload: outcomes.append((ok, payload)))

        with (
            patch.object(
                feishu_dialog.feishu,
                "resolve_existing_bitable",
                return_value=target,
            ),
            patch.object(
                feishu_dialog.feishu,
                "find_existing_bitable_text_values",
                side_effect=feishu.FeishuApiError(1, "飞书查重未完成。"),
            ),
            patch.object(
                feishu_dialog.feishu,
                "export_collection_to_new_bitable",
            ) as write,
        ):
            worker.run()

        self.assertEqual(outcomes, [(False, "飞书查重未完成。")])
        write.assert_not_called()

    def test_single_entry_routes_to_unified_session(self):
        parent = SimpleNamespace()
        records = (WorkArchiveRecord(title="作品"),)
        with patch("src.ui.feishu_save_dialog.FeishuSaveSession") as session_type:
            session = feishu_dialog.send_records_to_feishu(parent, _MemoryConfig(), records, title="作品")
            self.assertIs(session, session_type.return_value)
            self.assertEqual(session_type.call_args.args[2], records)
            session.start.assert_called_once()
            session.finished.connect.assert_called_once()

    def test_single_entry_prefers_its_saved_category_library(self):
        config = _MemoryConfig()
        config.feishu_table_targets = [
            {
                "target_id": "ai",
                "name": "AI 口播",
                "url": "https://example.feishu.cn/base/APPA?table=TBLA",
                "status": "ready",
            },
            {
                "target_id": "beauty",
                "name": "美妆",
                "url": "https://example.feishu.cn/base/APPB?table=TBLB",
                "status": "ready",
            },
        ]
        config.feishu_storage_preferences = {
            "single_target_id": "beauty",
            "selection_target_id": "",
            "recent_target_id": "ai",
        }

        def choose_current(dialog):
            self.assertEqual(
                [card.accessibleName() for card in dialog.option_cards],
                ["使用默认表格", "临时输入已有表格", "使用默认位置", "临时选择新建位置"],
            )
            dialog.option_cards[0].click()
            return dialog.result()

        with (
            patch.object(feishu_dialog.feishu, "is_configured", return_value=True),
            patch.object(feishu_dialog.feishu, "is_authorized", return_value=True),
            patch.object(
                feishu_dialog.FeishuActionDialog,
                "exec",
                new=choose_current,
            ),
        ):
            selected = feishu_dialog.choose_feishu_destination(
                None,
                config,
                context="single",
                suggested_name="新作品库",
            )

        self.assertEqual(selected["mode"], "existing")
        self.assertEqual(selected["target_id"], "beauty")
        self.assertEqual(selected["name"], "美妆")
        self.assertFalse(selected["temporary"])

    def test_custom_existing_table_is_one_off_and_does_not_replace_defaults(self):
        config = _MemoryConfig()
        config.feishu_table_targets = [
            {
                "target_id": "default",
                "name": "长期默认库",
                "url": "https://example.feishu.cn/base/APP0?table=TBL0",
                "status": "ready",
            }
        ]
        config.feishu_storage_preferences = {
            "existing_single_target_id": "default",
            "single_target_id": "default",
        }

        with (
            patch.object(feishu_dialog.feishu, "is_configured", return_value=True),
            patch.object(feishu_dialog.feishu, "is_authorized", return_value=True),
            patch.object(
                feishu_dialog.FeishuActionDialog,
                "exec",
                new=lambda dialog: (
                    dialog.option_cards[1].click() or dialog.result()
                ),
            ),
            patch.object(
                feishu_dialog.dialogs,
                "prompt",
                side_effect=[
                    "https://example.feishu.cn/base/APP1?table=TBL1",
                    "本次临时库",
                ],
            ),
        ):
            selection = feishu_dialog.choose_feishu_destination(
                None,
                config,
                context="single",
                suggested_name="作品库",
            )

        self.assertTrue(selection["temporary"])
        worker = SimpleNamespace(schema_version=1, registered_target=object())
        receipt = SimpleNamespace(
            url=selection["url"],
            destination_app_token="APP1",
            destination_table_id="TBL1",
            destination_table_name="本次临时库",
        )
        self.assertTrue(
            feishu_dialog.remember_feishu_destination(
                config, selection, worker, receipt
            )
        )
        self.assertIsNone(worker.registered_target)
        self.assertEqual(
            config.feishu_storage_preferences["existing_single_target_id"],
            "default",
        )
        self.assertEqual(len(config.feishu_table_targets), 1)

    def test_remembered_legacy_table_keeps_schema_truth_and_obsidian_untouched(self):
        config = _MemoryConfig()
        config.feishu_table_targets = [
            {
                "target_id": "first",
                "name": "AI 口播",
                "url": "https://example.feishu.cn/base/APPA?table=TBLA",
                "status": "ready",
            }
        ]
        worker = SimpleNamespace(schema_version=0, registered_target=None)
        receipt = SimpleNamespace(
            url="https://example.feishu.cn/base/APPB",
            destination_app_token="APPB",
            destination_table_id="TBLB",
            destination_table_name="旧版采集表",
        )
        selection = {
            "mode": "existing",
            "name": "长期素材库",
            "url": "https://example.feishu.cn/base/APPB?table=TBLB",
            "context": "selection",
        }

        saved = feishu_dialog.remember_feishu_destination(
            config, selection, worker, receipt
        )

        self.assertTrue(saved)
        self.assertEqual(config.obsidian_dir, "/vault/保持原样  ")
        self.assertEqual(len(config.feishu_table_targets), 2)
        registered = worker.registered_target
        self.assertEqual(registered["schema_version"], 0)
        self.assertEqual(registered["template_id"], "lulu.legacy-collection")
        self.assertEqual(registered["source_kind"], "existing")
        self.assertIn("table=TBLB", registered["url"])
        self.assertEqual(
            config.feishu_storage_preferences["selection_target_id"],
            registered["target_id"],
        )
        self.assertEqual(
            config.feishu_storage_preferences["existing_selection_target_id"],
            registered["target_id"],
        )

    def test_newly_created_table_becomes_a_reusable_concrete_table(self):
        config = _MemoryConfig()
        worker = SimpleNamespace(schema_version=1, registered_target=None)
        receipt = SimpleNamespace(
            url="https://example.feishu.cn/base/NEWAPP",
            destination_app_token="NEWAPP",
            destination_table_id="NEWTABLE",
            destination_table_name="Lulu 新建作品库",
        )
        selection = {
            "mode": "new",
            "source_kind": "created",
            "name": "Lulu 新建作品库",
            "context": "single",
        }

        self.assertTrue(
            feishu_dialog.remember_feishu_destination(
                config, selection, worker, receipt
            )
        )
        registered = worker.registered_target
        self.assertEqual(registered["source_kind"], "created")
        self.assertEqual(
            config.feishu_storage_preferences["existing_single_target_id"],
            registered["target_id"],
        )
        self.assertEqual(
            config.feishu_storage_preferences["single_target_id"],
            registered["target_id"],
        )

    def test_worker_falls_back_to_legacy_fields_without_mutating_the_table(self):
        record = WorkArchiveRecord(
            title="作品一",
            platform="douyin",
            work_id="123",
            summary="只应在标准表出现",
        )
        target = feishu.ExistingBitableTarget(
            "APP1",
            "TBL1",
            "旧版采集表",
            "https://example.feishu.cn/base/APP1?table=TBL1",
        )
        receipt = feishu.BitableExportReceipt(
            url=target.url,
            row_total=1,
            row_success=1,
            row_failed=0,
            row_unconfirmed=0,
            row_cancelled=0,
            cover_candidate=0,
            cover_success=0,
            cover_skipped=0,
            cover_failed=0,
            cover_uncertain=0,
            cancelled=False,
            destination_app_token="APP1",
            destination_table_id="TBL1",
            destination_table_name="旧版采集表",
        )
        worker = feishu_dialog.FeishuStorageWorker(
            (record,),
            {"mode": "existing", "url": target.url, "schema_version": 0},
            "不会新建",
        )
        with (
            patch.object(
                feishu_dialog.feishu,
                "resolve_existing_bitable",
                side_effect=[feishu.BitableTargetError("标准表不匹配"), target],
            ) as resolve,
            patch.object(
                feishu_dialog,
                "_archive_cover_preflight",
                return_value=((), ()),
            ),
            patch.object(
                feishu_dialog.feishu,
                "export_collection_to_new_bitable",
                return_value=receipt,
            ) as write,
        ):
            worker.run()

        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(worker.schema_version, 0)
        self.assertEqual(write.call_args.args[4], list(collection_export.BITABLE_FIELDS))
        fields = write.call_args.args[5][0]["fields"]
        self.assertEqual(set(fields).difference(collection_export.BITABLE_HEADERS), set())
        self.assertNotIn("摘要", fields)

    def test_unknown_or_standard_target_never_silently_downgrades_to_legacy(self):
        record = WorkArchiveRecord(title="作品一", platform="douyin", work_id="123")
        for schema_version in (None, 1):
            with self.subTest(schema_version=schema_version):
                worker = feishu_dialog.FeishuStorageWorker(
                    (record,),
                    {
                        "mode": "existing",
                        "url": "https://example.feishu.cn/base/APP1?table=TBL1",
                        "schema_version": schema_version,
                    },
                    "不应降级",
                )
                with (
                    patch.object(
                        feishu_dialog.feishu,
                        "resolve_existing_bitable",
                        side_effect=feishu.BitableTargetError("标准字段漂移"),
                    ) as resolve,
                    patch.object(
                        feishu_dialog.feishu,
                        "export_collection_to_new_bitable",
                    ) as write,
                ):
                    worker.run()
                self.assertEqual(resolve.call_count, 1)
                write.assert_not_called()
                self.assertEqual(worker.schema_version, 1)

    def test_repeated_click_returns_active_worker_before_opening_another_picker(self):
        active = SimpleNamespace(isRunning=lambda: True)
        parent = SimpleNamespace(_lulu_feishu_storage_worker=active)
        notices = []
        with patch.object(feishu_dialog, "choose_feishu_destination") as choose:
            returned = feishu_dialog.send_records_to_feishu(
                parent,
                _MemoryConfig(),
                (WorkArchiveRecord(title="作品一"),),
                title="作品一",
                toast=notices.append,
            )
        self.assertIs(returned, active)
        choose.assert_not_called()
        self.assertEqual(notices, ["上一份飞书存储还在进行，请稍候…"])

    def test_user_reachable_feishu_actions_do_not_call_docx_storage(self):
        ui_root = Path(feishu_dialog.__file__).parent
        for filename in (
            "feishu_dialog.py",
            "main_window.py",
            "library_page.py",
            "collection_page.py",
        ):
            source = (ui_root / filename).read_text(encoding="utf-8")
            self.assertNotIn("feishu.save_markdown", source, filename)
            self.assertNotIn("build_doc_markdown(", source, filename)


if __name__ == "__main__":
    unittest.main()
