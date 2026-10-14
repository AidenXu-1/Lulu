"""采集任务页面接线与管理浮层的离屏测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
import os
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHeaderView,
    QLineEdit,
    QLabel,
    QPushButton,
    QStyleOptionViewItem,
    QTableWidget,
    QWidget,
)

from src.core.collect.application import CollectionApplicationService, CollectionCommand  # noqa: E402
from src.core.collect.contracts import CollectResult  # noqa: E402
from src.core.collect.history import (  # noqa: E402
    CollectionHistoryRepository,
    CollectionRecord,
)
from src.core.collect.models import CollectedItem  # noqa: E402
from src.ui import theme as T  # noqa: E402
from src.ui.collection_history_dialog import (  # noqa: E402
    CollectionHistoryDialog,
    _record_markers,
)
from src.ui.collection_page import CollectionPage, _ROW_SELECTED_ROLE  # noqa: E402
from src.ui.control_foundation import PaperTextureFrame  # noqa: E402


XHS_URL = "https://www.xiaohongshu.com/user/profile/public_brand"


def item(
    work_id: str,
    author: str = "小红书作者",
    *,
    create_time: int = 0,
    unknown_time: bool = False,
) -> CollectedItem:
    extra = {"_field_state_duration_ms": "not_applicable"}
    if unknown_time:
        extra["_field_state_create_time"] = "unknown"
    elif create_time:
        extra["_field_state_create_time"] = "known"
    return CollectedItem(
        "",
        platform="xiaohongshu",
        work_id=work_id,
        author_name=author,
        title=f"作品 {work_id}",
        create_time=create_time,
        work_url=f"https://www.xiaohongshu.com/explore/{work_id}",
        content_kind="image",
        extra_stats=extra,
    )


class PureService:
    def __init__(self) -> None:
        self._pure = CollectionApplicationService(
            adapter_resolver=lambda _platform: (_ for _ in ()).throw(AssertionError()),
            session_factory=lambda *_args: (_ for _ in ()).throw(AssertionError()),
        )

    def inspect(self, raw_input: str):
        return self._pure.inspect(raw_input)

    def close_active_session(self):
        return True


class CollectionHistoryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        T.apply_theme("warm")
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "collection" / "history.json"
        self.repo = CollectionHistoryRepository(self.path)
        self.service = PureService()
        self.cfg = SimpleNamespace(
            collect_cover=False,
            feishu_target={},
            last_output_dir=str(Path(self.temp.name) / "exports"),
            media_dir=str(Path(self.temp.name) / "media"),
            save=lambda: True,
        )
        self.page = CollectionPage(
            self.cfg,
            collection_service=self.service,
            history_repository=self.repo,
        )
        self.page.resize(1200, 760)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.shutdown_workers()
        self.page.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def _deliver(self, result: CollectResult, *, mode: str = "profile_public") -> None:
        self.page._run_token += 1
        token = self.page._run_token
        command = CollectionCommand(XHS_URL, mode, profile_id="default")
        identity = self.service.inspect(XHS_URL).identity
        self.page._history_context[token] = (command, identity)
        self.page._on_collect_done(result, token)

    def _snapshot_with_records(
        self,
        count: int,
        *,
        long_first_name: bool = False,
        partial_first: bool = False,
    ):
        records = []
        for index in range(count):
            name = f"品牌公开主页第 {index + 1} 次采集"
            if index == 0 and long_first_name:
                name = "这是一个需要在窄窗口稳定省略但仍通过提示保留完整信息的超长采集任务名称" * 3
            records.append(
                CollectionRecord.create(
                    (item(f"fixture-{index}", "认证品牌"),),
                    platform="xiaohongshu",
                    homepage_url=XHS_URL,
                    source_method="profile_public",
                    completion="partial" if partial_first and index == 0 else "complete",
                    reason_code="rate_limited" if partial_first and index == 0 else "",
                    name=name,
                )
            )
        snapshot = self.repo.load()
        return replace(
            snapshot,
            active_records=tuple(records),
            selected_record_id=records[0].record_id if records else "",
        )

    def _assert_only_lulu_checkboxes(self, dialog: CollectionHistoryDialog) -> None:
        for index in range(dialog.list.count()):
            item = dialog.list.item(index)
            self.assertIsNone(item.data(Qt.ItemDataRole.CheckStateRole))
            self.assertFalse(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)
            option = QStyleOptionViewItem()
            dialog.list.itemDelegate().initStyleOption(
                option,
                dialog.list.indexFromItem(item),
            )
            self.assertFalse(
                option.features
                & QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
            )
            row = dialog.list.itemWidget(item)
            checks = row.findChildren(QLabel, "historyCheck")
            self.assertEqual(len(checks), 1)
            self.assertEqual(
                (checks[0].width(), checks[0].height()),
                (22, 22),
            )
            self.assertTrue(checks[0].accessibleName().startswith("选择任务："))

    def test_complete_partial_cancelled_and_confirmed_empty_become_independent_records(self):
        self._deliver(CollectResult.complete([item("one")]))
        self._deliver(
            CollectResult.partial(
                [item("two")], reason_code="rate_limited", next_action="稍后重试"
            )
        )
        self._deliver(CollectResult.cancelled([item("three")]))
        self._deliver(CollectResult.complete([], confirmed_empty=True))

        snapshot = self.repo.load()
        self.assertEqual(len(snapshot.active_records), 4)
        self.assertEqual(
            [row.completion for row in snapshot.active_records],
            ["complete", "cancelled", "partial", "complete"],
        )
        self.assertTrue(snapshot.active_records[0].confirmed_empty)
        self.assertEqual(len({row.record_id for row in snapshot.active_records}), 4)
        self.assertEqual(len(self.page.history_select.options), 4)
        labels = [label for _value, label in self.page.history_select.options]
        self.assertTrue(any("部分结果" in label for label in labels))
        self.assertTrue(any("含未获取字段" in label for label in labels))

    def test_selected_record_and_items_restore_across_page_restart(self):
        self._deliver(CollectResult.complete([item("old")]))
        first = self.repo.load().selected_record_id
        self._deliver(CollectResult.complete([item("new")]))
        self.page.history_select.set_value(first, emit=True)
        self.assertEqual([row.work_id for row in self.page._items], ["old"])

        other = CollectionPage(
            self.cfg,
            collection_service=self.service,
            history_repository=CollectionHistoryRepository(self.path),
        )
        try:
            self.assertEqual(other.history_select.currentData(), first)
            self.assertEqual([row.work_id for row in other._items], ["old"])
        finally:
            other.deleteLater()

    def test_history_selection_displays_reconciled_cover_truth(self):
        original = replace(
            item("cover-truth"),
            cover_url="https://assets.example.test/cover.jpg",
            cover_status="url_only",
        )
        self._deliver(CollectResult.complete([original]))
        record_id = self.repo.load().selected_record_id
        reconciled = replace(
            original,
            cover_status="downloaded",
            cover_local_path="xiaohongshu/cover-truth.jpg",
        )
        self.page._records_snapshot = SimpleNamespace(items=(reconciled,))

        self.page._on_history_selected(record_id)

        self.assertEqual(self.page._items[0].cover_status, "downloaded")
        self.assertEqual(
            self.page._items[0].cover_local_path,
            "xiaohongshu/cover-truth.jpg",
        )

    def test_history_selection_reads_latest_saved_work_status_on_first_switch(self):
        from src.core.collect.repository import CollectionRepository
        original = item("latest-status")
        self._deliver(CollectResult.complete([original]))
        record_id = self.repo.load().selected_record_id
        records = CollectionRepository(Path(self.temp.name) / "records.json")
        records.upsert([original])
        self.page._records_repo = records
        self.page._records_snapshot = records.load()
        records.compare_and_swap_fields(
            original.stable_key,
            expected={"transcript_status": original.transcript_status},
            updates={"transcript_status": "completed"},
        )
        records.compare_and_swap_fields(
            original.stable_key,
            expected={"media_status": original.media_status,
                      "media_local_path": original.media_local_path},
            updates={"media_status": "downloaded",
                     "media_local_path": "xiaohongshu/latest-status.mp4"},
        )

        self.page._on_history_selected(record_id)

        self.assertEqual(self.page._items[0].transcript_status, "completed")
        self.assertEqual(self.page._items[0].media_status, "downloaded")
        self.assertEqual(self.page._items[0].media_local_path, "xiaohongshu/latest-status.mp4")
        # Saved collection content remains a snapshot; runtime facts are overlaid.
        self.assertEqual(self.repo.load().selected_record.items[0].transcript_status,
                         original.transcript_status)

    def test_empty_cancel_and_failed_result_do_not_create_history(self):
        self._deliver(CollectResult.cancelled())
        self._deliver(CollectResult.failed("network_error"))
        self.assertEqual(self.repo.load().active_records, ())

    def test_readonly_future_schema_displays_rows_and_never_overwrites(self):
        self._deliver(CollectResult.complete([item("safe")]))
        document = json.loads(self.path.read_text("utf-8"))
        document["schema_version"] = 9
        self.path.write_text(json.dumps(document, ensure_ascii=False), "utf-8")
        before = self.path.read_bytes()
        page = CollectionPage(
            self.cfg,
            collection_service=self.service,
            history_repository=CollectionHistoryRepository(self.path),
        )
        try:
            self.assertFalse(page._history_snapshot.writable)
            page._run_token += 1
            token = page._run_token
            command = CollectionCommand(XHS_URL, "profile_public")
            page._history_context[token] = (command, self.service.inspect(XHS_URL).identity)
            page._on_collect_done(CollectResult.complete([item("current")]), token)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertIn("没有写入历史", page.status.text())
            self.assertEqual([row.work_id for row in page._items], ["current"])
        finally:
            page.deleteLater()

    def test_management_search_sort_rename_trash_restore_and_purge(self):
        self._deliver(CollectResult.complete([item("a", "苹果")]))
        self._deliver(CollectResult.complete([item("b", "小米")]))
        changed = []
        dialog = CollectionHistoryDialog(
            self.repo, self.repo.load(), on_changed=changed.append, parent=self.page
        )
        dialog.search_edit.setText("苹果")
        self.assertEqual(dialog.list.count(), 1)
        dialog.search_edit.clear()
        dialog.list.item(0).setCheckState(Qt.Checked)
        with patch("src.ui.collection_history_dialog.dialogs.prompt", return_value="新的记录名"):
            dialog.rename_selected()
        self.assertTrue(any(row.name == "新的记录名" for row in self.repo.load().active_records))

        dialog.list.item(0).setCheckState(Qt.Checked)
        with patch("src.ui.collection_history_dialog.dialogs.confirm", return_value=True):
            dialog.move_selected()
        self.assertEqual(len(self.repo.load().trashed_records), 1)
        dialog._set_bucket(True)
        dialog.list.item(0).setCheckState(Qt.Checked)
        dialog.move_selected()
        self.assertEqual(len(self.repo.load().trashed_records), 0)

        dialog._set_bucket(False)
        dialog.list.item(0).setCheckState(Qt.Checked)
        with patch("src.ui.collection_history_dialog.dialogs.confirm", return_value=True):
            dialog.move_selected()
        dialog._set_bucket(True)
        dialog.list.item(0).setCheckState(Qt.Checked)
        with patch("src.ui.collection_history_dialog.dialogs.confirm", return_value=True):
            dialog.purge_selected()
        self.assertEqual(len(self.repo.load().trashed_records), 0)
        self.assertTrue(changed)

    def test_home_toolbar_owns_the_only_four_data_location_entries(self):
        dialog = CollectionHistoryDialog(
            self.repo,
            self.repo.load(),
            parent=self.page,
        )
        self.assertEqual(dialog.title_label.text(), "采集任务管理")
        self.assertEqual(dialog.active_button.text(), "当前任务")
        self.assertEqual(dialog.trash_button.text(), "任务回收站")
        self.assertEqual(
            [label for _key, label in self.page.data_location_select.options],
            ["采集数据", "封面", "文稿", "音视频"],
        )
        self.assertFalse(hasattr(dialog, "data_location_button"))
        self.assertFalse(hasattr(dialog, "data_location_menu"))
        expected = {
            "collection": Path(self.temp.name) / "collection",
            "covers": Path(self.temp.name) / "collection" / "covers",
            "library": Path(self.cfg.last_output_dir),
            "media": Path(self.temp.name) / "collection" / "media",
        }
        self.assertFalse(expected["covers"].exists())
        self.assertFalse(expected["library"].exists())
        self.assertFalse(expected["media"].exists())
        opened = []
        with patch(
            "src.ui.collection_page.QDesktopServices.openUrl",
            side_effect=lambda url: opened.append(Path(url.toLocalFile())) or True,
        ), patch(
            "src.ui.collection_page.QFileDialog.getExistingDirectory",
            side_effect=AssertionError("受管目录不得打开选择器"),
        ):
            self.page._choose_data_location("collection")
            self.page._choose_data_location("covers")
        expected["library"].mkdir()
        expected["media"].mkdir()
        with patch(
            "src.ui.collection_page.QDesktopServices.openUrl",
            side_effect=lambda url: opened.append(Path(url.toLocalFile())) or True,
        ):
            self.page._open_data_location("library")
            self.page._open_data_location("media")
        self.assertEqual(opened, list(expected.values()))
        self.assertTrue(all(path.is_dir() for path in expected.values()))
        self.assertEqual(
            self.page.data_location_select.trigger.toolTip(),
            "已打开数据位置",
        )
        with patch(
            "src.ui.collection_page.QDesktopServices.openUrl",
            return_value=False,
        ):
            self.page._open_data_location("covers")
        self.assertEqual(
            self.page.data_location_select.trigger.toolTip(),
            "数据位置打开失败",
        )
        dialog.deleteLater()

    def test_data_location_picker_changes_only_future_user_defaults(self):
        original_library = Path(self.cfg.last_output_dir)
        original_library.mkdir()
        old_file = original_library / "既有文稿.txt"
        old_file.write_text("keep", encoding="utf-8")
        original_media = Path(self.cfg.media_dir)
        original_media.mkdir()
        old_media = original_media / "既有音频.mp3"
        old_media.write_bytes(b"keep")
        baseline_tooltip = self.page.data_location_select.trigger.toolTip()

        with patch(
            "src.ui.collection_page.QFileDialog.getExistingDirectory",
            return_value="",
        ):
            self.page._choose_data_location("library")
        self.assertEqual(self.cfg.last_output_dir, str(original_library))
        self.assertEqual(
            self.page.data_location_select.trigger.toolTip(),
            baseline_tooltip,
        )
        self.assertTrue(old_file.is_file())

        selected = Path(self.temp.name) / "future-library"
        selected.mkdir()
        save_calls = []
        self.cfg.save = lambda: save_calls.append(self.cfg.last_output_dir) or True
        with patch(
            "src.ui.collection_page.QFileDialog.getExistingDirectory",
            return_value=str(selected),
        ):
            self.page._choose_data_location("library")
        self.assertEqual(self.cfg.last_output_dir, str(selected))
        self.assertEqual(save_calls, [str(selected)])
        self.assertTrue(old_file.is_file())
        self.assertEqual(list(selected.iterdir()), [])
        library_row = next(
            row
            for row in self.page.data_location_select.popup.rows
            if row.key == "library"
        )
        self.assertIn(str(selected), library_row.open_button.toolTip())

        # 音视频是 Lulu 受管目录，只允许打开，不再允许改到用户路径。
        with patch(
            "src.ui.collection_page.QDesktopServices.openUrl",
            return_value=False,
        ), patch(
            "src.ui.collection_page.QFileDialog.getExistingDirectory",
            side_effect=AssertionError("受管音视频目录不得打开选择器"),
        ):
            self.page._choose_data_location("media")
        self.assertEqual(self.cfg.media_dir, str(original_media))
        self.assertTrue(old_media.is_file())
        self.assertEqual(
            self.page.data_location_select.trigger.toolTip(),
            "数据位置打开失败",
        )

    def test_v2_uses_paper_card_shared_controls_and_existing_theme_tokens(self):
        overlays = {
            "warm": "rgba(24,20,18,112)",
        }
        snapshot = self._snapshot_with_records(1)
        for theme in ("warm",):
            with self.subTest(theme=theme):
                T.apply_theme(theme)
                consumer = T.consumer_theme(theme)
                dialog = CollectionHistoryDialog(self.repo, snapshot, parent=self.page)
                self.assertIsInstance(dialog.card, PaperTextureFrame)
                self.assertEqual(dialog.property("consumerThemeScope"), "coreWorkspace")
                self.assertEqual(dialog.card.property("consumerThemeScope"), "coreWorkspace")
                self.assertEqual(dialog.property("historyOverlayColor"), overlays[theme])
                self.assertAlmostEqual(
                    dialog.card.property("paperTextureStrength"),
                    consumer.paper_texture_opacity,
                )
                self.assertIsInstance(
                    dialog.card.graphicsEffect(),
                    QGraphicsDropShadowEffect,
                )
                self.assertEqual(dialog.close_button.size().width(), 32)
                title_rule = dialog.findChild(QWidget, "studioTitleRule")
                self.assertIsNotNone(title_rule)
                self.assertEqual(title_rule.size().width(), 3)
                self.assertEqual(title_rule.size().height(), 22)
                self.assertEqual(dialog.search_edit.property("luluControl"), "textField")
                self.assertEqual(dialog.sort_select.property("consumerThemeScope"), "coreWorkspace")
                self.assertNotIn(
                    "改名",
                    [button.text() for button in dialog.findChildren(QPushButton)],
                )
                row = dialog.list.itemWidget(dialog.list.item(0))
                self.assertIsNotNone(row.edit_button)
                self.assertEqual(row.edit_button.property("luluControl"), "button")
                self.assertEqual(dialog.move_button.property("historyAction"), "danger")
                self.assertEqual(dialog.findChildren(QTableWidget), [])
                self.assertIn(consumer.surface_floating, dialog._history_local_qss)
                self.assertIn(consumer.accent, dialog._history_local_qss)
                self.assertIn(consumer.danger, dialog._history_local_qss)
                self.assertIn(
                    "QListWidget#historyList::item:hover",
                    dialog._history_local_qss,
                )
                self.assertIn(consumer.secondary_hover, dialog._history_local_qss)
                red, green, blue, _ = QColor(consumer.danger).getRgb()
                self.assertIn(
                    f"background: rgba({red},{green},{blue},18);",
                    dialog._history_local_qss,
                )
                self.assertIn(
                    f"border-color: rgba({red},{green},{blue},72);",
                    dialog._history_local_qss,
                )
                self.assertEqual(dialog.list.property("historyBucket"), "active")
                dialog.deleteLater()

    def test_inline_rename_targets_one_record_and_checkbox_or_blank_never_renames(self):
        snapshot = self._snapshot_with_records(2)
        dialog = CollectionHistoryDialog(self.repo, snapshot, parent=self.page)
        dialog.open()
        self.app.processEvents()
        item = dialog.list.item(0)
        record_id = str(item.data(Qt.UserRole))
        row = dialog.list.itemWidget(item)
        row.rename_requested.disconnect()
        rename_spy = QSignalSpy(row.rename_requested)
        toggle_spy = QSignalSpy(row.toggle_requested)

        with patch.object(dialog, "_rename_record") as rename_call:
            row.rename_requested.connect(rename_call)
            self.assertIsNotNone(row.edit_button)
            self.assertEqual(row.edit_button.toolTip(), "编辑这项任务的名称")
            self.assertEqual(
                row.edit_button.accessibleName(),
                f"编辑任务名称：{row.name_label.full_text}",
            )
            self.assertEqual(row.edit_button.focusPolicy(), Qt.FocusPolicy.StrongFocus)

            QTest.mouseDClick(row.name_label, Qt.MouseButton.LeftButton)
            self.assertEqual(rename_spy.count(), 1)
            self.assertEqual(rename_spy.at(0)[0], record_id)
            rename_call.assert_called_once_with(record_id)

            QTest.mouseDClick(row.check, Qt.MouseButton.LeftButton)
            QTest.mouseDClick(row, Qt.MouseButton.LeftButton, pos=QPoint(4, 60))
            self.assertEqual(rename_spy.count(), 1)

            QTest.mouseClick(row.check, Qt.MouseButton.LeftButton)
            self.assertEqual(toggle_spy.count(), 1)
            QTest.mouseClick(row, Qt.MouseButton.LeftButton, pos=QPoint(4, 60))
            self.assertEqual(toggle_spy.count(), 2)

            QTest.mouseClick(row.edit_button, Qt.MouseButton.LeftButton)
            self.assertEqual(rename_spy.count(), 2)
            self.assertEqual(rename_spy.at(1)[0], record_id)
            self.assertEqual(rename_call.call_count, 2)
            self.assertEqual(toggle_spy.count(), 2)
            row.edit_button.setFocus()
            QTest.keyClick(row.edit_button, Qt.Key.Key_Space)
            self.assertEqual(rename_spy.count(), 3)
            self.assertEqual(rename_call.call_count, 3)
            self.assertEqual(toggle_spy.count(), 2)
        dialog.reject()
        dialog.deleteLater()

    def test_record_rows_cover_default_selected_multi_search_markers_disabled_and_danger(self):
        snapshot = self._snapshot_with_records(3, partial_first=True)
        dialog = CollectionHistoryDialog(self.repo, snapshot, parent=self.page)
        dialog.open()
        self.app.processEvents()
        self.assertEqual(dialog.list.count(), 3)

        first_item = dialog.list.item(0)
        second_item = dialog.list.item(1)
        first_row = dialog.list.itemWidget(first_item)
        second_row = dialog.list.itemWidget(second_item)
        self._assert_only_lulu_checkboxes(dialog)
        self.assertFalse(first_row.property("checked"))
        self.assertFalse(second_row.property("checked"))

        first_position = dialog.list.visualItemRect(first_item).center()
        second_position = dialog.list.visualItemRect(second_item).center()
        QTest.mouseMove(dialog.list.viewport(), first_position)
        self.assertIs(dialog.list.itemAt(first_position), first_item)
        QTest.mouseClick(dialog.list.viewport(), Qt.LeftButton, pos=first_position)
        QTest.mouseClick(dialog.list.viewport(), Qt.LeftButton, pos=second_position)
        self.assertTrue(first_row.property("checked"))
        self.assertTrue(second_row.property("checked"))
        self.assertEqual(len(dialog.selected_ids()), 2)
        self.assertEqual(first_row.check.accessibleDescription(), "已选择")

        dialog.list.setCurrentItem(first_item)
        dialog.list.setFocus()
        QTest.keyClick(dialog.list, Qt.Key_Space)
        self.assertFalse(first_row.property("checked"))
        QTest.keyClick(dialog.list, Qt.Key_Space)
        self.assertTrue(first_row.property("checked"))

        QTest.keyClick(
            dialog.list,
            Qt.Key_A,
            Qt.ControlModifier | Qt.ShiftModifier,
        )
        self.assertEqual(dialog.selected_ids(), [])
        QTest.keyClick(dialog.list, Qt.Key_A, Qt.ControlModifier)
        self.assertEqual(len(dialog.selected_ids()), 3)
        dialog._set_all_items_checked(False)
        self.assertEqual(dialog.selected_ids(), [])
        first_item.setCheckState(Qt.Checked)
        self.assertEqual(dialog.selected_ids(), [first_item.data(Qt.UserRole)])
        self._assert_only_lulu_checkboxes(dialog)

        markers = first_row.findChildren(QLabel, "historyRecordMarker")
        self.assertEqual(
            {marker.text(): marker.property("markerKind") for marker in markers},
            {"部分结果": "warning", "含未获取字段": "neutral"},
        )

        dialog.search_edit.setText("认证品牌")
        self.assertEqual(dialog.list.count(), 3)
        self._assert_only_lulu_checkboxes(dialog)
        matched_row = dialog.list.itemWidget(dialog.list.item(0))
        matched_author = matched_row.findChild(QLabel, "historyRecordAuthor")
        self.assertTrue(matched_author.property("searchMatch"))

        readonly = replace(snapshot, writable=False, code="future_schema")
        readonly_dialog = CollectionHistoryDialog(self.repo, readonly, parent=self.page)
        readonly_row = readonly_dialog.list.itemWidget(readonly_dialog.list.item(0))
        self.assertIsNone(readonly_row.edit_button)
        self.assertFalse(readonly_dialog.move_button.isEnabled())
        self.assertIn("只读", readonly_dialog.readonly_label.text())

        trashed = replace(
            snapshot,
            active_records=(),
            trashed_records=snapshot.active_records,
            selected_record_id="",
        )
        trash_dialog = CollectionHistoryDialog(self.repo, trashed, parent=self.page)
        trash_dialog._set_bucket(True)
        self._assert_only_lulu_checkboxes(trash_dialog)
        self.assertEqual(trash_dialog.list.property("historyBucket"), "trash")
        for index in range(trash_dialog.list.count()):
            row = trash_dialog.list.itemWidget(trash_dialog.list.item(index))
            self.assertIsNone(row.edit_button)
        self.assertEqual(trash_dialog.move_button.property("historyAction"), "secondary")
        self.assertEqual(trash_dialog.purge_button.property("historyAction"), "danger")
        self.assertTrue(trash_dialog.purge_button.isVisibleTo(trash_dialog.card))

        dialog.reject()
        dialog.deleteLater()
        readonly_dialog.deleteLater()
        trash_dialog.deleteLater()

    def test_long_name_elides_visually_and_keeps_complete_tooltip(self):
        snapshot = self._snapshot_with_records(1, long_first_name=True)
        host = QWidget()
        host.resize(680, 500)
        host.show()
        dialog = CollectionHistoryDialog(self.repo, snapshot, parent=host)
        dialog.open()
        self.app.processEvents()
        row = dialog.list.itemWidget(dialog.list.item(0))
        name_label = row.findChild(QLabel, "historyRecordName")
        full_name = snapshot.active_records[0].name
        self.assertEqual(name_label.toolTip(), full_name)
        self.assertEqual(name_label.accessibleName(), full_name)
        self.assertNotEqual(name_label.text(), full_name)
        self.assertTrue(name_label.text().endswith("…"))
        dialog.reject()
        host.deleteLater()

    def test_list_counts_zero_one_eight_ten_and_hundreds_remain_scrollable(self):
        for count in (0, 1, 8, 10, 240):
            with self.subTest(count=count):
                dialog = CollectionHistoryDialog(
                    self.repo,
                    self._snapshot_with_records(count),
                    parent=self.page,
                )
                dialog.open()
                self.app.processEvents()
                self.assertEqual(dialog.list.count(), count)
                self.assertEqual(
                    [dialog.list.item(index).sizeHint().height() for index in range(count)],
                    [66] * count,
                )
                if count >= 8:
                    self.assertGreater(
                        dialog.list.verticalScrollBar().maximum(),
                        dialog.list.verticalScrollBar().minimum(),
                    )
                dialog.reject()
                dialog.deleteLater()

    def test_overlay_closes_by_x_escape_and_outside_without_effect_leak(self):
        self._deliver(CollectResult.complete([item("visible")]))
        parent = QWidget()
        parent.resize(1200, 760)
        parent.setStyleSheet("background: #f4eee6;")
        previous_focus = QLineEdit(parent)
        previous_focus.setGeometry(32, 32, 240, 38)
        effect = QGraphicsOpacityEffect(parent)
        parent.setGraphicsEffect(effect)
        parent.show()
        previous_focus.setFocus()
        self.app.processEvents()
        original_size = parent.size()

        for action in ("outside", "escape", "x"):
            with self.subTest(action=action):
                dialog = CollectionHistoryDialog(self.repo, self.repo.load(), parent=parent)
                dialog.open()
                self.app.processEvents()
                self.assertTrue(dialog.backdrop.has_scene())
                self.assertEqual(
                    dialog.backdrop.property("historyBackdropMode"),
                    "snapshot_softened",
                )
                self.assertLessEqual(dialog.card.width(), 680)
                self.assertLessEqual(dialog.card.height(), 500)
                self.assertGreaterEqual(dialog.width() - dialog.card.width(), 48)
                outside = dialog.grab().toImage().pixelColor(4, 4)
                self.assertNotEqual(outside, QColor("#ffffff"))
                self.assertGreater(outside.alpha(), 0)
                if action == "outside":
                    QTest.mouseClick(dialog, Qt.LeftButton, pos=QPoint(2, 2))
                elif action == "escape":
                    QTest.keyClick(dialog, Qt.Key_Escape)
                else:
                    QTest.mouseClick(dialog.close_button, Qt.LeftButton)
                self.app.processEvents()
                self.assertFalse(dialog.isVisible())
                self.assertIs(parent.graphicsEffect(), effect)
                self.assertIs(parent.focusWidget(), previous_focus)
                self.assertEqual(parent.size(), original_size)
                self.assertTrue(parent.isEnabled())
        parent.deleteLater()

    def test_overlay_stays_inside_one_native_window_and_tracks_parent_resize(self):
        parent = QWidget()
        parent.resize(1200, 760)
        parent.show()
        self.app.processEvents()
        parent_window_id = int(parent.winId())
        visible_top_levels_before = {
            widget
            for widget in QApplication.topLevelWidgets()
            if widget.isVisible() and widget.windowHandle() is not None
        }

        dialog = CollectionHistoryDialog(self.repo, self.repo.load(), parent=parent)
        self.assertNotIsInstance(dialog, QDialog)
        self.assertFalse(dialog.isWindow())
        self.assertFalse(dialog.windowFlags() & Qt.WindowType.Dialog)
        self.assertFalse(dialog.testAttribute(Qt.WidgetAttribute.WA_NativeWindow))
        self.assertIs(dialog.parentWidget(), parent)
        self.assertIs(dialog.window(), parent)
        self.assertEqual(
            {
                widget
                for widget in QApplication.topLevelWidgets()
                if widget.isVisible() and widget.windowHandle() is not None
            },
            visible_top_levels_before,
        )

        dialog.open()
        self.app.processEvents()
        self.assertEqual(
            {
                widget
                for widget in QApplication.topLevelWidgets()
                if widget.isVisible() and widget.windowHandle() is not None
            },
            visible_top_levels_before,
        )
        self.assertEqual(int(dialog.window().winId()), parent_window_id)
        self.assertEqual(dialog.geometry(), parent.rect())
        self.assertIsNone(QApplication.activeModalWidget())
        self.assertTrue(dialog.isAncestorOf(QApplication.focusWidget()))
        for _ in range(24):
            focused = QApplication.focusWidget()
            self.assertIsNotNone(focused)
            QTest.keyClick(focused, Qt.Key.Key_Tab)
            self.app.processEvents()
            self.assertTrue(dialog.isAncestorOf(QApplication.focusWidget()))

        parent.resize(1480, 900)
        self.app.processEvents()
        self.assertEqual(dialog.geometry(), parent.rect())
        self.assertEqual((dialog.card.width(), dialog.card.height()), (680, 500))
        self.assertEqual(
            {
                widget
                for widget in QApplication.topLevelWidgets()
                if widget.isVisible() and widget.windowHandle() is not None
            },
            visible_top_levels_before,
        )

        dialog.reject()
        self.app.processEvents()
        self.assertEqual(
            {
                widget
                for widget in QApplication.topLevelWidgets()
                if widget.isVisible() and widget.windowHandle() is not None
            },
            visible_top_levels_before,
        )
        self.assertEqual(int(parent.winId()), parent_window_id)
        parent.deleteLater()

    def test_page_manager_uses_inline_overlay_and_page_hide_closes_it(self):
        self._deliver(CollectResult.complete([item("inline-page")]))
        visible_top_levels_before = {
            widget
            for widget in QApplication.topLevelWidgets()
            if widget.isVisible() and widget.windowHandle() is not None
        }
        page_window_id = int(self.page.window().winId())

        self.page._open_history_manager()
        self.app.processEvents()
        overlay = self.page._history_dialog
        self.assertIsNotNone(overlay)
        self.assertFalse(overlay.isWindow())
        self.assertIs(overlay.parentWidget(), self.page.window())
        self.assertEqual(int(overlay.window().winId()), page_window_id)
        self.assertEqual(
            {
                widget
                for widget in QApplication.topLevelWidgets()
                if widget.isVisible() and widget.windowHandle() is not None
            },
            visible_top_levels_before,
        )

        self.page.hide()
        self.app.processEvents()
        self.assertIsNone(self.page._history_dialog)
        self.assertFalse(overlay.isVisible())
        self.assertTrue(
            {
                widget
                for widget in QApplication.topLevelWidgets()
                if widget.isVisible() and widget.windowHandle() is not None
            }.issubset(visible_top_levels_before)
        )

    def test_cover_markers_distinguish_downloaded_and_recovery_block(self):
        base = CollectionRecord.create(
            (item("cover-marker"),),
            platform="xiaohongshu",
            homepage_url=XHS_URL,
            source_method="profile_public",
            completion="complete",
        )
        downloaded = replace(
            base,
            items=(
                replace(
                    base.items[0],
                    cover_url="https://assets.example.test/cover.jpg",
                    cover_status="downloaded",
                    cover_local_path="xiaohongshu/cover.jpg",
                ),
            ),
        )
        self.assertIn("封面 1/1", _record_markers(downloaded))

        uncertain = replace(
            base,
            items=(
                replace(
                    base.items[0],
                    cover_url="https://assets.example.test/cover.jpg",
                    cover_status="failed",
                    last_error_code="cover:cover_commit_uncertain",
                ),
            ),
        )
        self.assertIn("封面待恢复", _record_markers(uncertain))

    def test_paper_and_680_by_500_or_1200_by_760_keep_safe_margin(self):
        self._deliver(CollectResult.complete([item("theme")]))
        for theme in ("warm",):
            for size in ((680, 500), (1200, 760), (1480, 900)):
                with self.subTest(theme=theme, size=size):
                    T.apply_theme(theme)
                    host = QWidget()
                    host.resize(*size)
                    host.show()
                    dialog = CollectionHistoryDialog(self.repo, self.repo.load(), parent=host)
                    dialog.open()
                    self.app.processEvents()
                    self._assert_only_lulu_checkboxes(dialog)
                    self.assertGreaterEqual(dialog.width() - dialog.card.width(), 48)
                    self.assertGreaterEqual(dialog.height() - dialog.card.height(), 48)
                    expected_card = (
                        (632, 452) if size == (680, 500) else (680, 500)
                    )
                    self.assertEqual(
                        (dialog.card.width(), dialog.card.height()),
                        expected_card,
                    )
                    self.assertTrue(dialog.backdrop.has_scene())
                    self.assertNotEqual(
                        dialog.grab().toImage().pixelColor(4, 4),
                        QColor("#ffffff"),
                    )
                    self.assertEqual(dialog.list.property("historyBucket"), "active")
                    dialog._set_bucket(True)
                    self.assertEqual(dialog.list.property("historyBucket"), "trash")
                    consumer = T.consumer_theme(theme)
                    red, green, blue, _ = QColor(consumer.danger).getRgb()
                    self.assertIn(
                        f"background: rgba({red},{green},{blue},18);",
                        dialog._history_local_qss,
                    )
                    dialog._set_bucket(False)
                    self.assertEqual(dialog.list.property("historyBucket"), "active")
                    dialog.reject()
                    host.deleteLater()

    def test_beijing_date_shortcuts_custom_range_and_unknown_truth(self):
        shanghai = ZoneInfo("Asia/Shanghai")

        def stamp(year, month, day, hour=12):
            return int(datetime(year, month, day, hour, tzinfo=shanghai).timestamp())

        rows = [
            item("today", create_time=stamp(2026, 1, 1)),
            item("day-6", create_time=stamp(2025, 12, 26)),
            item("day-7", create_time=stamp(2025, 12, 25)),
            item("day-29", create_time=stamp(2025, 12, 3)),
            item("day-89", create_time=stamp(2025, 10, 4)),
            item("day-90", create_time=stamp(2025, 10, 3)),
            item("unknown", unknown_time=True),
        ]
        with patch("src.ui.collection_page._beijing_today", return_value=date(2026, 1, 1)):
            self.page._set_items(rows)
            self.assertEqual(len(self.page._view), 7)

            self.page.date_select.set_value("7", emit=True)
            self.assertEqual(
                {row.work_id for row in self.page._view},
                {"today", "day-6"},
            )
            self.page.date_select.set_value("30", emit=True)
            self.assertEqual(
                {row.work_id for row in self.page._view},
                {"today", "day-6", "day-7", "day-29"},
            )
            self.page.date_select.set_value("90", emit=True)
            self.assertEqual(
                {row.work_id for row in self.page._view},
                {"today", "day-6", "day-7", "day-29", "day-89"},
            )

            self.page.date_end.setDate(QDate(2026, 1, 1))
            self.page.date_start.setDate(QDate(2025, 12, 31))
            self.page.date_select.set_value("custom", emit=True)
            self.assertEqual(
                {row.work_id for row in self.page._view},
                {"today"},
            )
            self.page.date_start.setDate(QDate(2026, 1, 2))
            self.assertEqual(
                self.page._custom_date_range,
                (date(2026, 1, 2), date(2026, 1, 2)),
            )
            self.page.date_end.setDate(QDate(2025, 12, 30))
            self.assertEqual(
                self.page._custom_date_range,
                (date(2025, 12, 30), date(2025, 12, 30)),
            )
            self.assertEqual(self.page.status.text(), "")
            self.page.date_select.set_value("all", emit=True)
            self.assertIn("unknown", {row.work_id for row in self.page._view})

    def test_stable_key_reducer_shift_cmd_ctrl_visible_all_and_hidden_count(self):
        rows = [
            item(f"row-{index}", create_time=1_700_000_000 + index)
            for index in range(6)
        ]
        self.page._set_items(rows)
        keys = [row.stable_key for row in self.page._view]

        self.page._reduce_selection(keys[0])
        self.assertTrue(self.page.transcribe_btn.isEnabled())
        self.page._reduce_selection(keys[2], modifiers=Qt.ShiftModifier)
        self.assertEqual(self.page._selected_keys, set(keys[:3]))
        self.page._reduce_selection(keys[1], modifiers=Qt.MetaModifier)
        self.page._reduce_selection(keys[3], modifiers=Qt.ControlModifier)
        self.assertEqual(
            self.page._selected_keys,
            {keys[0], keys[2], keys[3]},
        )

        selected_work_id = next(
            row.work_id for row in rows if row.stable_key == keys[0]
        )
        self.page.filter_edit.setText(selected_work_id)
        self.assertEqual([row.stable_key for row in self.page._view], [keys[0]])
        self.assertIn("其中 2 条隐藏", self.page.count_label.text())
        self.page._toggle_select_all()
        self.assertEqual(self.page._selected_keys, {keys[2], keys[3]})
        self.page._toggle_select_all()
        self.assertEqual(
            self.page._selected_keys,
            {keys[0], keys[2], keys[3]},
        )

        hidden_anchor_target = keys[-1]
        hidden_work_id = next(
            row.work_id for row in rows if row.stable_key == hidden_anchor_target
        )
        self.page.filter_edit.setText(hidden_work_id)
        self.page._reduce_selection(
            hidden_anchor_target,
            modifiers=Qt.ShiftModifier,
        )
        self.assertEqual(self.page._selection_anchor_key, hidden_anchor_target)
        self.assertIn(hidden_anchor_target, self.page._selected_keys)
        self.page._set_items([item("other-record")])
        self.assertEqual(self.page._selected_keys, set())
        self.assertEqual(self.page._selection_anchor_key, "")

    def test_checkbox_and_whole_row_use_one_reducer_path(self):
        self.page._set_items([item("one"), item("two")])
        self.app.processEvents()
        first = self.page.table.item(0, 0)
        first.setCheckState(Qt.Checked)
        self.assertEqual(
            self.page._selected_keys,
            {self.page._view[0].stable_key},
        )
        first.setCheckState(Qt.Unchecked)
        self.assertEqual(self.page._selected_keys, set())

        rect = self.page.table.visualItemRect(self.page.table.item(1, 2))
        QTest.mouseClick(
            self.page.table.viewport(),
            Qt.LeftButton,
            pos=rect.center(),
        )
        self.assertEqual(
            self.page._selected_keys,
            {self.page._view[1].stable_key},
        )

    def test_selected_keys_paint_and_clear_the_entire_visible_row(self):
        self.page._set_items([item("highlight-one"), item("highlight-two")])
        selected_key = self.page._view[0].stable_key

        self.page._reduce_selection(selected_key)

        for column in range(self.page.table.columnCount()):
            cell = self.page.table.item(0, column)
            self.assertIsNotNone(cell)
            self.assertTrue(cell.data(_ROW_SELECTED_ROLE))
        for column in range(self.page.table.columnCount()):
            self.assertFalse(self.page.table.item(1, column).data(_ROW_SELECTED_ROLE))

        self.page._reduce_selection(selected_key)

        for column in range(self.page.table.columnCount()):
            self.assertFalse(self.page.table.item(0, column).data(_ROW_SELECTED_ROLE))

    def test_selected_row_background_is_opaque_and_continuous_across_columns(self):
        self.page._set_items([item("wash-one"), item("wash-two")])
        self.page._reduce_selection(self.page._view[0].stable_key)
        delegate = self.page.table.itemDelegate()
        expected = delegate.selected_background()
        self.assertEqual(expected.alpha(), 255)
        self.assertNotEqual(expected, QColor(self.page._consumer.surface_focal))

        for column in range(self.page.table.columnCount()):
            with self.subTest(column=column):
                selected_cell = self.page.table.item(0, column)
                normal_cell = self.page.table.item(1, column)
                self.page.table.scrollToItem(selected_cell)
                self.app.processEvents()
                selected_rect = self.page.table.visualItemRect(selected_cell)
                normal_rect = self.page.table.visualItemRect(normal_cell)
                self.assertFalse(selected_rect.isEmpty())
                image = self.page.table.viewport().grab().toImage()
                ratio = image.devicePixelRatio()

                def sample(rect):
                    x = max(rect.left() + 2, rect.right() - 5)
                    y = rect.top() + 4
                    return image.pixelColor(round(x * ratio), round(y * ratio))

                selected_color = sample(selected_rect)
                normal_color = sample(normal_rect)
                expected_distance = sum(
                    abs(a - b)
                    for a, b in zip(
                        selected_color.getRgb()[:3], expected.getRgb()[:3]
                    )
                )
                contrast = sum(
                    abs(a - b)
                    for a, b in zip(
                        selected_color.getRgb()[:3], normal_color.getRgb()[:3]
                    )
                )
                self.assertLessEqual(expected_distance, 12)
                self.assertGreaterEqual(contrast, 18)

    def test_double_click_media_and_cover_reveals_only_validated_files(self):
        row_item = replace(
            item("reveal-local-file"),
            content_kind="video",
            extra_stats={},
            media_status="downloaded",
            media_local_path="xiaohongshu/media.mp4",
            cover_status="downloaded",
            cover_local_path="xiaohongshu/cover.jpg",
        )
        self.page._set_items([row_item])
        media_path = Path(self.temp.name) / "managed-media.mp4"
        cover_path = Path(self.temp.name) / "managed-cover.jpg"

        def double_click(column: int) -> None:
            cell = self.page.table.item(0, column)
            self.page.table.scrollToItem(cell)
            self.app.processEvents()
            QTest.mouseClick(
                self.page.table.viewport(),
                Qt.LeftButton,
                pos=self.page.table.visualItemRect(cell).center(),
            )
            QTest.mouseDClick(
                self.page.table.viewport(),
                Qt.LeftButton,
                pos=self.page.table.visualItemRect(cell).center(),
            )
            self.app.processEvents()

        with (
            patch.object(self.page, "_managed_media_file", return_value=media_path),
            patch.object(self.page, "_managed_cover_file", return_value=cover_path),
            patch("src.ui.collection_page.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            double_click(10)
            double_click(11)

        self.assertEqual(
            run.call_args_list,
            [
                unittest.mock.call(["open", "-R", str(media_path)], check=False),
                unittest.mock.call(["open", "-R", str(cover_path)], check=False),
            ],
        )
        self.assertEqual(self.page._selected_keys, {row_item.stable_key})
        self.assertIn("封面", self.page.status.text())

        with (
            patch.object(self.page, "_managed_media_file", return_value=None),
            patch("src.ui.collection_page.subprocess.run") as blocked_run,
        ):
            self.page._on_table_cell_double_clicked(0, 10)

        blocked_run.assert_not_called()
        self.assertIn("重新下载", self.page.status.text())

    def test_legacy_item_trash_is_read_only_and_preserved_by_task_operations(self):
        seeded = self.repo.add_record(
            (item("keep"), item("legacy-trash")),
            platform="xiaohongshu",
            homepage_url=XHS_URL,
            source_method="profile_public",
            completion="complete",
        )
        record_id = seeded.selected_record_id
        seeded = self.repo.trash_items(
            record_id,
            ["xiaohongshu:legacy-trash"],
            expected_source_hash=seeded.source_hash,
        )
        before = seeded.selected_record.item_trash

        dialog = CollectionHistoryDialog(
            self.repo,
            self.repo.load(),
            on_changed=self.page._on_history_changed,
            parent=self.page,
        )
        self.assertFalse(hasattr(dialog, "item_trash_button"))
        self.assertFalse(hasattr(dialog, "_set_item_trash_bucket"))
        self.assertFalse(hasattr(self.page, "delete_btn"))
        self.assertEqual(
            dialog.legacy_item_trash_label.text(),
            "检测到旧版回收作品 1 条，数据已只读保留。",
        )
        with patch(
            "src.ui.collection_history_dialog.dialogs.prompt",
            return_value="改名后任务",
        ):
            dialog._rename_record(record_id)
        self.assertEqual(self.repo.load().selected_record.item_trash, before)
        dialog.list.item(0).setCheckState(Qt.Checked)
        with patch(
            "src.ui.collection_history_dialog.dialogs.confirm",
            return_value=True,
        ):
            dialog.move_selected()
        after_trash = self.repo.load()
        self.assertEqual(after_trash.trashed_records[0].item_trash, before)
        dialog._set_bucket(True)
        dialog.list.item(0).setCheckState(Qt.Checked)
        dialog.move_selected()
        self.assertEqual(self.repo.load().active_records[0].item_trash, before)
        dialog.deleteLater()

    def test_field_filter_operator_matrix_keeps_unknown_out_and_zero_truthful(self):
        def metric_item(work_id: str, like: int | None) -> CollectedItem:
            extra = {"_field_state_duration_ms": "not_applicable"}
            value = 0
            if like is None:
                extra["_field_state_like"] = "unknown"
            else:
                extra["_field_state_like"] = "known"
                value = like
            return CollectedItem(
                "",
                platform="xiaohongshu",
                work_id=work_id,
                author_name="筛选作者",
                title=f"作品 {work_id}",
                like=value,
                work_url=f"https://www.xiaohongshu.com/explore/{work_id}",
                content_kind="image",
                extra_stats=extra,
            )

        rows = [
            metric_item("zero", 0),
            metric_item("five", 5),
            metric_item("ten", 10),
            metric_item("unknown", None),
        ]
        self.page._set_items(rows)
        numeric_expected = {
            "gt": {"ten"},
            "ge": {"five", "ten"},
            "eq": {"five"},
            "ne": {"zero", "ten"},
            "lt": {"zero"},
            "le": {"zero", "five"},
        }
        self.page.metric_combo.setCurrentIndex(
            self.page.metric_combo.findText("点赞")
        )
        self.assertEqual(
            {
                self.page.operator_combo.itemText(index)
                for index in range(self.page.operator_combo.count())
            },
            {
                "大于 >", "大于等于 ≥", "等于 =",
                "不等于 ≠", "小于 <", "小于等于 ≤",
            },
        )
        for operator, expected in numeric_expected.items():
            with self.subTest(operator=operator):
                self.page.operator_combo.setCurrentIndex(
                    self.page.operator_combo.findData(operator)
                )
                self.page.metric_value.setText("5")
                self.assertEqual(
                    {entry.work_id for entry in self.page._view},
                    expected,
                )
                self.assertNotIn(
                    "unknown",
                    {entry.work_id for entry in self.page._view},
                )

        self.page.operator_combo.setCurrentIndex(
            self.page.operator_combo.findData("ge")
        )
        self.page.metric_value.setText("5")
        valid = [entry.work_id for entry in self.page._view]
        self.page.metric_value.setText("-1")
        self.assertEqual([entry.work_id for entry in self.page._view], valid)
        self.assertFalse(hasattr(self.page, "filter_error"))
        self.assertEqual(self.page.metric_value.property("state"), "error")
        self.assertEqual(
            self.page.metric_value.toolTip(),
            "请输入有效的非负数字",
        )
        self.assertEqual(
            self.page.metric_value.accessibleDescription(),
            "请输入有效的非负数字",
        )
        self.assertEqual(self.page.status.text(), "")

        text_rows = [
            replace(rows[0], title="Alpha"),
            replace(rows[1], title="Beta"),
            replace(rows[2], title=""),
        ]
        self.page._set_items(text_rows)
        self.page.metric_combo.setCurrentIndex(
            self.page.metric_combo.findText("作品标题")
        )
        self.assertEqual(
            [
                self.page.operator_combo.itemText(index)
                for index in range(self.page.operator_combo.count())
            ],
            ["包含", "不包含", "等于", "不等于"],
        )
        text_expected = {
            "contains": {"zero"},
            "not_contains": {"five"},
            "eq": {"zero"},
            "ne": {"five"},
        }
        for operator, expected in text_expected.items():
            with self.subTest(text_operator=operator):
                self.page.operator_combo.setCurrentIndex(
                    self.page.operator_combo.findData(operator)
                )
                self.page.metric_value.setText("Alpha")
                self.assertEqual(
                    {entry.work_id for entry in self.page._view},
                    expected,
                )
                self.assertNotIn(
                    "ten",
                    {entry.work_id for entry in self.page._view},
                )

        self.page.metric_combo.setCurrentIndex(
            self.page.metric_combo.findText("内容类型")
        )
        self.assertEqual(
            [
                self.page.operator_combo.itemText(index)
                for index in range(self.page.operator_combo.count())
            ],
            ["等于", "不等于"],
        )
        self.assertTrue(self.page.enum_value_combo.isVisible())
        self.assertFalse(self.page.metric_value.isVisible())

    def test_h2_controls_fit_paper_dual_sizes_with_resizable_result_table(self):
        for theme in ("warm",):
            for size in ((1200, 760), (1480, 900)):
                with self.subTest(theme=theme, size=size):
                    T.apply_theme(theme)
                    page = CollectionPage(
                        self.cfg,
                        collection_service=self.service,
                        history_repository=self.repo,
                    )
                    page.resize(*size)
                    page.show()
                    page.date_select.set_value("custom", emit=True)
                    page._set_items([
                        item(f"{theme}-{index}", create_time=1_700_000_000 + index)
                        for index in range(240)
                    ])
                    self.app.processEvents()
                    self.assertTrue(page.date_start.isVisible())
                    self.assertTrue(page.date_end.isVisible())
                    self.assertTrue(page.date_connector.isVisible())
                    self.assertEqual(page.date_connector.text(), "至")
                    for editor in (page.date_start, page.date_end):
                        text_width = editor.editor.fontMetrics().horizontalAdvance(
                            editor.editor.text()
                        )
                        self.assertGreaterEqual(
                            editor.width(),
                            editor.minimumSizeHint().width(),
                        )
                        self.assertGreaterEqual(editor.editor.width(), text_width + 8)
                        self.assertFalse(editor.trigger.icon().isNull())
                    self.assertEqual(
                        page.table.horizontalScrollBarPolicy(),
                        Qt.ScrollBarAsNeeded,
                    )
                    header = page.table.horizontalHeader()
                    self.assertEqual(
                        header.sectionResizeMode(0),
                        QHeaderView.Fixed,
                    )
                    self.assertEqual(page.table.columnWidth(0), 34)
                    for column in range(1, page.table.columnCount()):
                        self.assertEqual(
                            header.sectionResizeMode(column),
                            QHeaderView.Interactive,
                        )
                    self.assertFalse(header.sectionsMovable())
                    original_title_width = page.table.columnWidth(2)
                    page.table.setColumnWidth(2, original_title_width + 48)
                    self.assertEqual(
                        page.table.columnWidth(2),
                        original_title_width + 48,
                    )
                    page.table.setFixedWidth(720)
                    self.app.processEvents()
                    self.assertGreater(
                        page.table.horizontalScrollBar().maximum(),
                        page.table.horizontalScrollBar().minimum(),
                    )
                    self.assertEqual(
                        page.table.horizontalHeaderItem(11).text(),
                        "封面",
                    )
                    self.assertGreaterEqual(page.table.columnWidth(11), 68)
                    page.metric_combo.setCurrentIndex(
                        page.metric_combo.findText("点赞")
                    )
                    widest_operator = max(
                        page.operator_combo.fontMetrics().horizontalAdvance(
                            page.operator_combo.itemText(index)
                        )
                        for index in range(page.operator_combo.count())
                    )
                    self.assertGreaterEqual(
                        page.operator_combo.width(),
                        widest_operator + 24,
                    )
                    page.metric_combo.setCurrentIndex(
                        page.metric_combo.findText("作品标题")
                    )
                    self.assertEqual(page.operator_combo.count(), 4)
                    self.assertGreater(page.table.viewport().height(), 0)
                    page.shutdown_workers()
                    page.deleteLater()


if __name__ == "__main__":
    unittest.main()
