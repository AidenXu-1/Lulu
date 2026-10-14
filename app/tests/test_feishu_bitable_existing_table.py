"""已有飞书多维表格追加链路：只使用假 HTTP，不读真实凭据。"""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from src.core import feishu  # noqa: E402
from src.core.collect import export  # noqa: E402


def compatible_fields() -> list[dict]:
    return [
        {
            "field_id": f"fld{index}",
            "field_name": payload["field_name"],
            "type": payload["type"],
        }
        for index, payload in enumerate(
            feishu._bitable_field_payload(field) for field in export.BITABLE_FIELDS
        )
    ]


class ExistingBitableTargetTests(unittest.TestCase):
    def test_cloud_drive_creation_uses_base_root_without_doc_or_wiki_api(self):
        with (
            patch.object(feishu, "_user_token", return_value="USER"),
            patch.object(
                feishu,
                "_api",
                return_value={
                    "app": {
                        "app_token": "APP-DRIVE",
                        "url": "https://example.feishu.cn/base/APP-DRIVE",
                    }
                },
            ) as api,
        ):
            created = feishu.create_drive_bitable("Lulu 主页采集")

        api.assert_called_once_with(
            "POST",
            "/bitable/v1/apps",
            "USER",
            json={"name": "Lulu 主页采集"},
        )
        self.assertEqual(created["app_token"], "APP-DRIVE")
        self.assertEqual(created["node_token"], "")
        self.assertIn("/base/APP-DRIVE", created["url"])

    def test_cloud_drive_export_never_calls_wiki_creation(self):
        with (
            patch.object(feishu, "_user_token", return_value="USER"),
            patch.object(
                feishu,
                "create_drive_bitable",
                return_value={
                    "app_token": "APP-DRIVE",
                    "node_token": "",
                    "url": "https://example.feishu.cn/base/APP-DRIVE",
                },
            ) as create_drive,
            patch.object(feishu, "create_wiki_bitable") as create_wiki,
            patch.object(
                feishu,
                "_prepare_initial_bitable_table",
                return_value="DEFAULT",
            ) as prepare_table,
            patch.object(feishu, "delete_bitable_table") as delete_table,
            patch.object(
                feishu,
                "batch_create_records",
                return_value=feishu.BitableBatchReceipt(1, 1, 0),
            ),
        ):
            receipt = feishu.export_collection_to_new_bitable(
                "",
                "",
                "Lulu 主页采集",
                "作品库",
                export.BITABLE_FIELDS,
                ({"fields": {"标题": "作品一"}},),
                destination_kind="drive",
            )

        create_drive.assert_called_once_with("Lulu 主页采集")
        create_wiki.assert_not_called()
        prepare_table.assert_called_once_with(
            "APP-DRIVE", "作品库", export.BITABLE_FIELDS, "USER"
        )
        delete_table.assert_not_called()
        self.assertEqual(receipt.destination_app_token, "APP-DRIVE")
        self.assertEqual(receipt.destination_table_id, "DEFAULT")

    def test_same_table_write_lock_serializes_competing_workers(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()

        def first():
            with feishu._bitable_table_write_lock("APP1", "TBL1"):
                first_entered.set()
                release_first.wait(1.0)

        def second():
            second_started.set()
            with feishu._bitable_table_write_lock("APP1", "TBL1"):
                second_entered.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        try:
            first_thread.start()
            self.assertTrue(first_entered.wait(1.0))
            second_thread.start()
            self.assertTrue(second_started.wait(1.0))
            self.assertFalse(second_entered.wait(0.05))
        finally:
            release_first.set()
            first_thread.join(1.0)
            second_thread.join(1.0)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertTrue(second_entered.is_set())

    def test_text_value_lookup_filters_in_cloud_and_follows_pagination(self):
        calls: list[tuple] = []
        first = "https://www.douyin.com/video/1"
        second = "https://www.douyin.com/video/2"
        third = "https://www.douyin.com/video/3"

        def fake_api(method, path, token, *, json=None):
            calls.append((method, path, token, json))
            if "page_token=" not in path:
                return {
                    "items": [
                        {"fields": {"原作品链接": {"link": first, "text": "作品一"}}},
                        {"fields": {"原作品链接": [{"link": second, "text": "作品二"}]}},
                    ],
                    "has_more": True,
                    "page_token": "NEXT PAGE",
                }
            return {
                "items": [{"fields": {"原作品链接": "https://example.com/9"}}],
                "has_more": False,
            }

        with patch.object(feishu, "_api", side_effect=fake_api):
            found = feishu.find_existing_bitable_text_values(
                "APP1",
                "TBL1",
                "原作品链接",
                (first, second, third, first),
                token="USER",
            )

        self.assertEqual(found, {first, second})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:3], (
            "POST",
            "/bitable/v1/apps/APP1/tables/TBL1/records/search?page_size=500",
            "USER",
        ))
        self.assertIn("page_token=NEXT%20PAGE", calls[1][1])
        self.assertEqual(calls[0][3]["field_names"], ["原作品链接"])
        self.assertEqual(calls[0][3]["filter"]["conjunction"], "or")
        self.assertEqual(
            [condition["value"] for condition in calls[0][3]["filter"]["conditions"]],
            [[first], [second], [third]],
        )

    def test_text_value_lookup_chunks_large_key_sets_at_fifty(self):
        bodies = []

        def fake_api(_method, _path, _token, *, json=None):
            bodies.append(json)
            return {"items": [], "has_more": False}

        with patch.object(feishu, "_api", side_effect=fake_api):
            found = feishu.find_existing_bitable_text_values(
                "APP1",
                "TBL1",
                "原作品链接",
                (f"https://www.douyin.com/video/{index}" for index in range(51)),
                token="USER",
            )

        self.assertEqual(found, set())
        self.assertEqual(
            [len(body["filter"]["conditions"]) for body in bodies],
            [50, 1],
        )

    def test_base_link_resolves_exact_table_and_validates_fields(self):
        url = "https://example.feishu.cn/base/APP1?table=TBL1&view=VIEW1"
        with (
            patch.object(feishu, "_user_token", return_value="USER"),
            patch.object(
                feishu,
                "_list_all_bitable_tables",
                return_value=[{"table_id": "TBL1", "name": "长期素材库"}],
            ) as list_tables,
            patch.object(
                feishu,
                "_list_bitable_fields",
                return_value=compatible_fields(),
            ) as list_fields,
            patch.object(feishu, "_api") as api,
        ):
            target = feishu.resolve_existing_bitable(url, export.BITABLE_FIELDS)

        self.assertEqual(
            target,
            feishu.ExistingBitableTarget("APP1", "TBL1", "长期素材库", url),
        )
        list_tables.assert_called_once_with("APP1", "USER")
        list_fields.assert_called_once_with("APP1", "TBL1", "USER")
        api.assert_not_called()

    def test_wiki_link_resolves_real_app_token_before_table_check(self):
        url = "https://example.feishu.cn/wiki/NODE1?table=TBL1"
        with (
            patch.object(feishu, "_user_token", return_value="USER"),
            patch.object(
                feishu,
                "_api",
                return_value={"node": {"obj_type": "bitable", "obj_token": "APP1"}},
            ) as api,
            patch.object(
                feishu,
                "_list_all_bitable_tables",
                return_value=[{"table_id": "TBL1", "name": "采集结果"}],
            ),
            patch.object(
                feishu,
                "_list_bitable_fields",
                return_value=compatible_fields(),
            ),
        ):
            target = feishu.resolve_existing_bitable(url, export.BITABLE_FIELDS)

        self.assertEqual(target.app_token, "APP1")
        self.assertEqual(target.table_id, "TBL1")
        self.assertIn("/wiki/v2/spaces/get_node?token=NODE1", api.call_args.args[1])

    def test_invalid_or_ambiguous_link_stops_before_credentials_or_http(self):
        fixtures = (
            "http://example.feishu.cn/base/APP1?table=TBL1",
            "https://evil.example/base/APP1?table=TBL1",
            "https://evil.example@example.feishu.cn/base/APP1?table=TBL1",
            "https://example.feishu.cn/base/APP1",
            "https://example.feishu.cn/docx/DOC1?table=TBL1",
            "https://example.feishu.cn/base/APP1?table=TBL1" + ("x" * 4096),
        )
        for source in fixtures:
            with self.subTest(source=source), patch.object(feishu, "_user_token") as token:
                with self.assertRaises(feishu.BitableTargetError):
                    feishu.resolve_existing_bitable(source, export.BITABLE_FIELDS)
                token.assert_not_called()

    def test_missing_or_wrong_type_fields_are_zero_write(self):
        fields = compatible_fields()
        fields = [field for field in fields if field["field_name"] != "转录文稿"]
        next(field for field in fields if field["field_name"] == "点赞")["type"] = 1
        with (
            patch.object(feishu, "_user_token", return_value="USER"),
            patch.object(
                feishu,
                "_list_all_bitable_tables",
                return_value=[{"table_id": "TBL1", "name": "字段冲突表"}],
            ),
            patch.object(feishu, "_list_bitable_fields", return_value=fields),
            patch.object(feishu, "batch_create_records") as write_records,
        ):
            with self.assertRaises(feishu.BitableTargetError) as raised:
                feishu.resolve_existing_bitable(
                    "https://example.feishu.cn/base/APP1?table=TBL1",
                    export.BITABLE_FIELDS,
                )

        message = str(raised.exception)
        self.assertIn("缺少字段：转录文稿", message)
        self.assertIn("同名字段类型不一致：点赞", message)
        self.assertIn("没有写入", message)
        write_records.assert_not_called()

    def test_table_listing_follows_pagination_without_looping(self):
        calls: list[str] = []

        def fake_api(_method, path, _token):
            calls.append(path)
            if "page_token=" not in path:
                return {
                    "items": [{"table_id": "TBL0"}],
                    "has_more": True,
                    "page_token": "NEXT PAGE",
                }
            return {"items": [{"table_id": "TBL1"}], "has_more": False}

        with patch.object(feishu, "_api", side_effect=fake_api):
            tables = feishu._list_all_bitable_tables("APP1", "USER")

        self.assertEqual([table["table_id"] for table in tables], ["TBL0", "TBL1"])
        self.assertIn("page_token=NEXT%20PAGE", calls[1])

    def test_existing_target_reuses_batch_append_and_never_creates_or_deletes_table(self):
        target = feishu.ExistingBitableTarget(
            "APP1",
            "TBL1",
            "长期素材库",
            "https://example.feishu.cn/base/APP1?table=TBL1",
        )
        records = ({"fields": {"标题": "一"}}, {"fields": {"标题": "二"}})
        with (
            patch.object(feishu, "_user_token", return_value="USER"),
            patch.object(feishu, "create_wiki_bitable") as create_node,
            patch.object(feishu, "_list_bitable_tables") as list_tables,
            patch.object(
                feishu,
                "_list_bitable_fields",
                return_value=compatible_fields(),
            ),
            patch.object(feishu, "create_bitable_table") as create_table,
            patch.object(feishu, "delete_bitable_table") as delete_table,
            patch.object(
                feishu,
                "batch_create_records",
                return_value=feishu.BitableBatchReceipt(2, 2, 0),
            ) as append,
        ):
            receipt = feishu.export_collection_to_new_bitable(
                "",
                "",
                "不会使用的标题",
                "采集结果",
                export.BITABLE_FIELDS,
                records,
                existing_target=target,
            )

        create_node.assert_not_called()
        list_tables.assert_not_called()
        create_table.assert_not_called()
        delete_table.assert_not_called()
        append.assert_called_once()
        self.assertEqual(append.call_args.args[:2], ("APP1", "TBL1"))
        self.assertEqual(append.call_args.args[2], list(records))
        self.assertEqual((receipt.row_success, receipt.row_total), (2, 2))
        self.assertEqual(receipt.url, target.url)

    def test_existing_target_rechecks_schema_at_write_boundary(self):
        target = feishu.ExistingBitableTarget(
            "APP1",
            "TBL1",
            "长期素材库",
            "https://example.feishu.cn/base/APP1?table=TBL1",
        )
        fields = [
            field for field in compatible_fields() if field["field_name"] != "音视频附件"
        ]
        with (
            patch.object(feishu, "_user_token", return_value="USER"),
            patch.object(feishu, "_list_bitable_fields", return_value=fields),
            patch.object(feishu, "batch_create_records") as append,
        ):
            with self.assertRaises(feishu.BitableTargetError):
                feishu.export_collection_to_new_bitable(
                    "",
                    "",
                    "不会使用的标题",
                    "采集结果",
                    export.BITABLE_FIELDS,
                    ({"fields": {"标题": "一"}},),
                    existing_target=target,
                )
        append.assert_not_called()


if __name__ == "__main__":
    unittest.main()
