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


if __name__ == "__main__":
    unittest.main()
