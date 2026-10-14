"""P1A 统一模型、保守路由、结果合同与内存迁移的纯逻辑测试。"""

from __future__ import annotations

from copy import deepcopy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.collect.contracts import (  # noqa: E402
    CollectRequest,
    CollectResult,
    LinkIdentity,
    adapter_capability,
)
from core.collect.models import CollectedItem, dedupe, from_aweme  # noqa: E402
from core.collect.repository import MigrationError, migrate_records  # noqa: E402
from core.collect.router import detect_link  # noqa: E402


class TestUnifiedModel(unittest.TestCase):
    def test_legacy_constructor_keeps_douyin_identity(self):
        item = CollectedItem("123", author_name="作者")
        self.assertEqual(item.schema_version, 2)
        self.assertEqual(item.platform, "douyin")
        self.assertEqual(item.work_id, "123")
        self.assertEqual(item.stable_key, "douyin:123")

    def test_cross_platform_same_id_is_not_duplicate(self):
        items = [
            CollectedItem("7"),
            CollectedItem("", platform="bilibili", work_id="7"),
            CollectedItem("", platform="xiaohongshu", work_id="7"),
        ]
        self.assertEqual([item.stable_key for item in dedupe(items)], [
            "douyin:7", "bilibili:7", "xiaohongshu:7",
        ])

    def test_duplicate_stable_key_keeps_first(self):
        first = CollectedItem("7", title="first")
        second = CollectedItem("7", title="second")
        self.assertEqual(dedupe([first, second]), [first])

    def test_empty_ids_are_preserved_not_merged(self):
        first = CollectedItem("")
        second = CollectedItem("")
        self.assertEqual(dedupe([first, second]), [first, second])

    def test_non_douyin_must_not_fake_aweme_id(self):
        with self.assertRaisesRegex(ValueError, "非抖音"):
            CollectedItem("fake", platform="bilibili", work_id="BV1xx")

    def test_unknown_platform_and_state_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知采集平台"):
            CollectedItem("", platform="unknown", work_id="1")
        with self.assertRaisesRegex(ValueError, "未知本地资源状态"):
            CollectedItem("1", cover_status="surprise")

    def test_to_dict_contains_v2_identity(self):
        data = CollectedItem("9").to_dict()
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["platform"], "douyin")
        self.assertEqual(data["work_id"], "9")

    def test_existing_aweme_parser_populates_v2_identity(self):
        item = from_aweme({"aweme_id": "88", "author": {"sec_uid": "author-1"}})
        self.assertEqual(item.stable_key, "douyin:88")
        self.assertEqual(item.author_id, "author-1")


class TestRouter(unittest.TestCase):
    def test_douyin_homepage(self):
        result = detect_link("https://www.douyin.com/user/MS4w.LjAB_abc?from=share#x")
        self.assertEqual((result.platform, result.kind), ("douyin", "homepage"))
        self.assertEqual(result.extracted_id, "MS4w.LjAB_abc")
        self.assertEqual(result.canonical_url, "https://www.douyin.com/user/MS4w.LjAB_abc")

    def test_douyin_work(self):
        result = detect_link("https://www.douyin.com/video/741234")
        self.assertEqual((result.platform, result.kind, result.extracted_id),
                         ("douyin", "work", "741234"))

    def test_douyin_short_link_is_probable_without_expansion(self):
        result = detect_link("https://v.douyin.com/Ab_C1/")
        self.assertEqual((result.platform, result.kind, result.confidence),
                         ("douyin", "unknown", "probable"))

    def test_xiaohongshu_homepage(self):
        result = detect_link("https://www.xiaohongshu.com/user/profile/abc_123")
        self.assertEqual((result.platform, result.kind, result.extracted_id),
                         ("xiaohongshu", "homepage", "abc_123"))

    def test_xiaohongshu_work_inside_share_text(self):
        result = detect_link(
            "看看这篇 https://www.xiaohongshu.com/explore/note-9?xsec_token=secret 很棒。"
        )
        self.assertEqual((result.platform, result.kind), ("xiaohongshu", "work"))
        self.assertNotIn("xsec_token", result.canonical_url)

    def test_xiaohongshu_short_link(self):
        result = detect_link("https://xhslink.com/aBcDe")
        self.assertEqual((result.platform, result.kind, result.confidence),
                         ("xiaohongshu", "unknown", "probable"))

    def test_bilibili_homepage(self):
        result = detect_link("https://space.bilibili.com/123456/video")
        self.assertEqual(result.platform, "bilibili")
        self.assertEqual(result.kind, "unknown")
        exact = detect_link("https://space.bilibili.com/123456")
        self.assertEqual((exact.kind, exact.extracted_id), ("homepage", "123456"))

    def test_bilibili_work_and_short_link(self):
        work = detect_link("https://www.bilibili.com/video/BV1Ab411c7mD/")
        short = detect_link("https://b23.tv/a1B2")
        self.assertEqual((work.platform, work.kind), ("bilibili", "work"))
        self.assertEqual((short.platform, short.kind), ("bilibili", "unknown"))

    def test_unknown_and_lookalike_hosts(self):
        self.assertEqual(detect_link("没有链接").reason_code, "invalid_input")
        lookalike = detect_link("https://bilibili.com.evil.test/video/BV1xx")
        self.assertEqual((lookalike.platform, lookalike.reason_code),
                         ("unknown", "unsupported_link"))

    def test_ambiguous_supported_links_fail_closed(self):
        result = detect_link(
            "https://www.douyin.com/video/1 https://www.bilibili.com/video/BV1xx"
        )
        self.assertEqual((result.platform, result.reason_code),
                         ("unknown", "ambiguous_link"))

    def test_repeated_same_link_is_not_ambiguous(self):
        url = "https://www.douyin.com/video/1"
        result = detect_link(f"{url} 再发一次 {url}")
        self.assertEqual((result.platform, result.kind), ("douyin", "work"))

    def test_credentials_and_unusual_port_are_rejected(self):
        credentials = detect_link("https://user:pass@www.douyin.com/video/1")
        unusual_port = detect_link("https://www.douyin.com:8443/video/1")
        self.assertEqual(credentials.platform, "unknown")
        self.assertEqual(unusual_port.platform, "unknown")

    def test_router_never_resolves_dns(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("network used")):
            result = detect_link("https://space.bilibili.com/123")
        self.assertEqual(result.kind, "homepage")


class TestCollectContracts(unittest.TestCase):
    def setUp(self):
        self.item = CollectedItem("1")

    def test_adapter_registry_marks_formally_ready_platforms(self):
        self.assertTrue(adapter_capability("douyin").available)
        self.assertFalse(adapter_capability("bilibili").available)
        self.assertIn("unverified", adapter_capability("bilibili").reason)
        xiaohongshu = adapter_capability("xiaohongshu")
        self.assertTrue(xiaohongshu.available)
        self.assertEqual(xiaohongshu.reason, "formal_adapter_ready")
        self.assertEqual(xiaohongshu.modes, ("profile_public",))

    def test_collect_request_rejects_unknown_platform_and_bad_limit(self):
        with self.assertRaises(ValueError):
            CollectRequest(LinkIdentity(), mode="public")
        identity = LinkIdentity("douyin", "homepage", "https://www.douyin.com/user/a",
                                "a", "exact")
        with self.assertRaises(ValueError):
            CollectRequest(identity, mode="public", limit=0)

    def test_complete_with_items(self):
        result = CollectResult.complete([self.item])
        self.assertEqual(result.completion, "complete")
        self.assertEqual(result.items, (self.item,))

    def test_empty_complete_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(ValueError, "真实为空"):
            CollectResult.complete([])
        self.assertTrue(CollectResult.complete([], confirmed_empty=True).confirmed_empty)

    def test_partial_requires_items_reason_and_next_action(self):
        with self.assertRaisesRegex(ValueError, "至少一条"):
            CollectResult.partial([], reason_code="rate_limited", next_action="稍后重试")
        with self.assertRaisesRegex(ValueError, "next_action"):
            CollectResult.partial([self.item], reason_code="rate_limited", next_action="")
        result = CollectResult.partial(
            [self.item], reason_code="rate_limited", next_action="稍后重试", cursor={"page": 2}
        )
        self.assertEqual(result.completion, "partial")
        self.assertEqual(result.cursor["page"], 2)

    def test_failed_cannot_hide_existing_items(self):
        with self.assertRaisesRegex(ValueError, "partial"):
            CollectResult(items=(self.item,), completion="failed", reason_code="network_error")

    def test_cancelled_preserves_items(self):
        result = CollectResult.cancelled([self.item])
        self.assertEqual((result.completion, result.items), ("cancelled", (self.item,)))

    def test_unknown_reason_code_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知 reason"):
            CollectResult.failed("made_up")

    def test_cancelled_reason_cannot_masquerade_as_failed(self):
        with self.assertRaisesRegex(ValueError, "只能用于"):
            CollectResult.failed("cancelled")


class TestMemoryMigration(unittest.TestCase):
    def test_v1_douyin_record_migrates_to_v2(self):
        source = [{"aweme_id": "1", "title": "旧记录", "like": 3}]
        before = deepcopy(source)
        result = migrate_records(source)
        self.assertEqual(source, before)
        self.assertEqual((result.migrated_count, result.already_v2_count), (1, 0))
        self.assertEqual(result.items[0].stable_key, "douyin:1")
        self.assertEqual(result.items[0].title, "旧记录")

    def test_v2_record_is_validated_without_downgrade(self):
        source = [{
            "schema_version": 2,
            "aweme_id": "",
            "platform": "bilibili",
            "work_id": "BV1xx",
            "title": "B站作品",
        }]
        result = migrate_records(source)
        self.assertEqual((result.migrated_count, result.already_v2_count), (0, 1))
        self.assertEqual(result.items[0].stable_key, "bilibili:BV1xx")

    def test_v2_to_dict_round_trips_in_memory(self):
        source = [CollectedItem("", platform="xiaohongshu", work_id="note-1").to_dict()]
        result = migrate_records(source)
        self.assertEqual(result.items[0].stable_key, "xiaohongshu:note-1")
        self.assertEqual(result.already_v2_count, 1)

    def test_explicit_v1_schema_is_supported(self):
        source = [{"schema_version": 1, "platform": "douyin", "aweme_id": "2"}]
        result = migrate_records(source)
        self.assertEqual(result.items[0].stable_key, "douyin:2")

    def test_empty_batch_is_valid_and_has_zero_counts(self):
        result = migrate_records([])
        self.assertEqual(result.items, ())
        self.assertEqual((result.migrated_count, result.already_v2_count), (0, 0))

    def test_duplicate_key_fails_without_mutating_source(self):
        source = [{"aweme_id": "1"}, {"aweme_id": "1", "title": "重复"}]
        before = deepcopy(source)
        with self.assertRaisesRegex(MigrationError, "重复"):
            migrate_records(source)
        self.assertEqual(source, before)

    def test_empty_legacy_id_fails_without_mutating_source(self):
        source = [{"aweme_id": "", "title": "无法识别"}]
        before = deepcopy(source)
        with self.assertRaisesRegex(MigrationError, "缺少 aweme_id"):
            migrate_records(source)
        self.assertEqual(source, before)

    def test_unknown_field_fails_without_mutating_source(self):
        source = [{"aweme_id": "1", "raw_cookie": "secret"}]
        before = deepcopy(source)
        with self.assertRaisesRegex(MigrationError, "未知字段"):
            migrate_records(source)
        self.assertEqual(source, before)

    def test_non_mapping_fails_without_partial_result(self):
        with self.assertRaisesRegex(MigrationError, "不是字典"):
            migrate_records([{"aweme_id": "1"}, "bad"])  # type: ignore[list-item]


if __name__ == "__main__":
    unittest.main()
