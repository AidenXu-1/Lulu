from __future__ import annotations

from datetime import datetime, timezone
import unittest

from src.core.collect import export as collection_export
from src.core.collect.models import CollectedItem, FIELD_STATE_PREFIX
from src.core.feishu_storage import (
    LEGACY_TRANSIENT_MEDIA_FIELD,
    STANDARD_BITABLE_FIELDS,
    STANDARD_LIBRARY_TEMPLATE_V1,
    WorkArchiveRecord,
    dedupe_archive_records,
    extract_transcript_body,
    make_library_target,
    make_work_key,
    normalize_work_url,
    preferred_library_target,
    record_from_collected_item,
    record_from_transcript,
    record_to_bitable_fields,
    transcript_meta_from_collected_item,
    upsert_library_target,
)
from src.core.models import Transcript


class StandardTemplateTests(unittest.TestCase):
    def test_v1_matches_the_thirteen_homepage_collection_fields(self):
        self.assertEqual(STANDARD_BITABLE_FIELDS[0][0], "标题")
        standard_by_name = {
            field[0]: field[1]
            for field in STANDARD_BITABLE_FIELDS
        }
        legacy_by_name = {
            field[0]: field[1]
            for field in collection_export.BITABLE_FIELDS
        }
        self.assertEqual(
            {name: standard_by_name.get(name) for name in legacy_by_name},
            legacy_by_name,
        )
        self.assertEqual(len(STANDARD_BITABLE_FIELDS), 13)
        self.assertEqual(set(standard_by_name), set(legacy_by_name))
        self.assertEqual(LEGACY_TRANSIENT_MEDIA_FIELD, "音视频直链")

    def test_v1_field_names_are_unique_and_exclude_internal_identity(self):
        names = STANDARD_LIBRARY_TEMPLATE_V1.field_names
        self.assertEqual(len(names), len(set(names)))
        for internal in ("平台", "作品ID", "Lulu作品键", "Lulu写入ID", "模板版本"):
            self.assertNotIn(internal, names)


class WorkIdentityTests(unittest.TestCase):
    def test_url_normalization_removes_global_tracking_but_preserves_identity_query(self):
        raw = (
            "HTTPS://Example.COM:443/a/%7eitem/"
            "?utm_source=share&b=2&xsec_token=secret&a=1#reader"
        )
        self.assertEqual(
            normalize_work_url(raw),
            "https://example.com/a/~item?a=1&b=2&xsec_token=secret",
        )

    def test_url_normalization_rejects_non_http_and_embedded_credentials(self):
        self.assertEqual(normalize_work_url("file:///private/example.mp4"), "")
        self.assertEqual(normalize_work_url("https://user:secret@example.com/a"), "")

    def test_generic_identity_query_is_preserved_but_known_platform_tracking_is_removed(self):
        self.assertNotEqual(
            normalize_work_url("https://example.com/work?si=one"),
            normalize_work_url("https://example.com/work?si=two"),
        )
        self.assertEqual(
            normalize_work_url("https://youtu.be/video?si=one"),
            normalize_work_url("https://youtu.be/video?si=two"),
        )

    def test_work_key_prefers_platform_identity_and_falls_back_to_url_digest(self):
        self.assertEqual(
            make_work_key(" BiliBili ", "BV1AbC", "https://ignored.example"),
            "bilibili:BV1AbC",
        )
        first = make_work_key(
            "",
            "",
            "https://example.com/work/1?utm_source=a&x=2",
        )
        second = make_work_key("", "", "https://EXAMPLE.com/work/1/?x=2#top")
        self.assertTrue(first.startswith("url:"))
        self.assertEqual(first, second)


class CollectedItemAdapterTests(unittest.TestCase):
    def test_collected_item_preserves_current_values_and_stable_transcript_lookup(self):
        item = CollectedItem(
            aweme_id="123",
            author_name="小泽",
            title="作品标题",
            create_time=1_784_500_000,
            duration_ms=87_000,
            like=10,
            collect=20,
            comment=30,
            share=40,
            play_url="https://media.example/video.mp4",
            extra_stats={"view": 900},
        )
        record = record_from_collected_item(
            item,
            {"douyin:123": "稳定键文稿", "123": "旧键文稿"},
            captured_at_ms=1_800_000_000_000,
        )
        self.assertEqual(record.work_key, "douyin:123")
        self.assertEqual(record.transcript_text, "稳定键文稿")
        self.assertEqual(record.duration_seconds, 87)
        self.assertEqual(record.view_count, 900)

        fields = record_to_bitable_fields(
            record,
            allowed_fields=collection_export.BITABLE_HEADERS,
        )
        self.assertEqual(fields["作者昵称"], "小泽")
        self.assertEqual(fields["点赞"], 10)
        self.assertEqual(fields["时长"], "1:27")
        self.assertEqual(fields["转录文稿"], "稳定键文稿")
        self.assertNotIn("平台", fields)
        self.assertNotIn("播放", fields)

    def test_collected_unknown_numbers_remain_absent_instead_of_false_zero(self):
        states = {
            f"{FIELD_STATE_PREFIX}{name}": "unknown"
            for name in ("create_time", "duration_ms", "like", "collect", "comment", "share")
        }
        item = CollectedItem(
            "",
            platform="xiaohongshu",
            work_id="note-1",
            title="图文",
            content_kind="image",
            extra_stats=states,
        )
        fields = record_to_bitable_fields(record_from_collected_item(item))
        for field in ("发布日期", "点赞", "收藏", "评论", "分享"):
            self.assertNotIn(field, fields)
        self.assertEqual(fields["时长"], "图文")

    def test_collected_facts_can_be_merged_into_batch_transcript_library_meta(self):
        item = CollectedItem(
            aweme_id="735002",
            author_name="作者甲",
            title="批量转录作品",
            create_time=1_785_888_000,
            duration_ms=95_400,
            like=12,
            collect=8,
            comment=3,
            share=2,
            play_url="https://media.example/735002.mp4",
            author_id="author-1",
            extra_stats={"author_fans": "1万+"},
        )

        meta = transcript_meta_from_collected_item(item)

        self.assertEqual(meta["author"]["name"], "作者甲")
        self.assertEqual(meta["author"]["fans"], "1万+")
        self.assertEqual(
            meta["stats"],
            {"like": 12, "favorite": 8, "comment": 3, "share": 2},
        )
        self.assertEqual(meta["work_id"], "735002")
        self.assertEqual(meta["original_url"], "https://www.douyin.com/video/735002")
        self.assertEqual(meta["media_url"], "https://media.example/735002.mp4")
        self.assertEqual(meta["duration"], 95.4)


class TranscriptAdapterTests(unittest.TestCase):
    def test_markdown_archive_exports_only_transcript_section_and_keeps_timestamps(self):
        markdown = """---
title: 测试作品
source: https://www.douyin.com/video/1
---

# 测试作品

## 作品信息 / YAML

作者：测试作者

## 简介

这是不应该存入逐字稿列的简介。

## 逐字稿

**[00:00]** 第一句

**[00:03]** 第二句

## 总结

这是总结。
"""
        transcript = Transcript(text="原始文本", title="测试作品")

        record = record_from_transcript(transcript, text_override=markdown)

        self.assertEqual(
            record.transcript_text,
            "**[00:00]** 第一句\n\n**[00:03]** 第二句",
        )
        self.assertNotIn("作品信息", record.transcript_text)
        self.assertNotIn("简介", record.transcript_text)
        self.assertNotIn("总结", record.transcript_text)

    def test_structured_markdown_without_transcript_never_falls_back_to_full_body(self):
        transcript = Transcript(text="原始纯逐字稿")

        record = record_from_transcript(
            transcript,
            text_override=(
                "---\ntitle: 测试\n---\n"
                "# 作品信息\n不应写入飞书\n\n# 内容摘要\n也不应写入"
            ),
        )

        self.assertEqual(record.transcript_text, "")

    def test_segment_timestamps_are_preserved_without_markdown_override(self):
        from src.core.models import Segment

        transcript = Transcript(
            text="第一句 第二句",
            segments=[
                Segment(text="第一句", start=0.0, end=2.0),
                Segment(text="第二句", start=3.0, end=5.0),
            ],
        )

        record = record_from_transcript(
            transcript,
            text_override="第一句 第二句",
        )

        self.assertEqual(record.transcript_text, "[00:00] 第一句\n[00:03] 第二句")
        self.assertEqual(extract_transcript_body("纯逐字稿\n第二行"), "纯逐字稿\n第二行")

    def test_transcript_aliases_fill_date_duration_original_and_media_urls(self):
        transcript = Transcript(
            text="完整元数据",
            source="https://v.douyin.com/share",
            saved_media="/managed/video.mp4",
            meta={
                "original_url": "https://www.douyin.com/video/735002",
                "play_url": "https://v26.douyinvod.com/video.mp4",
                "create_time": 1785686400,
                "duration_ms": 95400,
                "platform": "douyin",
                "work_id": "735002",
            },
        )

        record = record_from_transcript(transcript)
        fields = record_to_bitable_fields(record)

        self.assertEqual(record.work_url, "https://www.douyin.com/video/735002")
        self.assertEqual(record.media_url, "https://v26.douyinvod.com/video.mp4")
        self.assertEqual(record.duration_seconds, 95.4)
        self.assertEqual(record.media_local_path, "/managed/video.mp4")
        self.assertIn("发布日期", fields)
        self.assertEqual(fields["时长"], "1:35")
        self.assertEqual(
            fields["原作品链接"]["link"],
            "https://www.douyin.com/video/735002",
        )
        self.assertEqual(
            fields["音视频直链"]["link"],
            "https://v26.douyinvod.com/video.mp4",
        )

    def test_transcript_maps_available_metadata_and_keeps_edited_and_original_text(self):
        transcript = Transcript(
            text="原始逐字稿",
            language="zh",
            engine="SenseVoice",
            source="https://www.douyin.com/video/987?utm_source=share",
            title="真实标题",
            saved_media="/managed/media.mp4",
            meta={
                "description": "作品简介",
                "upload_date": "20260803",
                "duration": 65.4,
                "webpage_url": "https://www.douyin.com/video/987?utm_source=share",
                "tags": ["口播", "AI", "口播"],
                "stats": {
                    "like": 12,
                    "favorite": 8,
                    "comment": 3,
                    "share": 2,
                    "view": 500,
                },
                "author": {
                    "name": "作者甲",
                    "handle": "author_1",
                    "fans": "1万+",
                    "likes_total": 120_000,
                    "homepage_url": "https://www.douyin.com/user/author_1",
                },
                "cover_platform": "douyin",
                "cover_work_id": "987",
                "cover_url": "https://cover.example/987.jpg",
                "cover_status": "url_only",
            },
        )
        record = record_from_transcript(
            transcript,
            text_override="用户整理后的文案",
            captured_at_ms=1_800_000_000_000,
        )
        self.assertEqual(record.work_key, "douyin:987")
        self.assertEqual(record.transcript_text, "用户整理后的文案")
        self.assertEqual(record.original_transcript_text, "原始逐字稿")
        self.assertEqual(record.tags, ("口播", "AI"))
        self.assertEqual(
            record.published_at_ms,
            int(datetime(2026, 8, 3, tzinfo=timezone.utc).timestamp() * 1000),
        )

        fields = record_to_bitable_fields(record, write_id="job-1:987")
        self.assertEqual(fields["作者昵称"], "作者甲")
        self.assertEqual(fields["粉丝数"], "1万+")
        self.assertEqual(fields["收藏"], 8)
        self.assertEqual(fields["时长"], "1:05")
        self.assertEqual(fields["来源入口"], "单条转录")
        self.assertEqual(fields["Lulu写入ID"], "job-1:987")
        self.assertEqual(fields["模板版本"], "1")

    def test_local_transcript_uses_filename_but_has_no_false_remote_identity(self):
        transcript = Transcript(
            text="本地文稿",
            source="/Users/test/访谈素材.wav",
            meta={},
        )
        record = record_from_transcript(transcript)
        self.assertEqual(record.title, "访谈素材")
        self.assertEqual(record.work_url, "")
        self.assertEqual(record.work_key, "")


class RecordMappingAndDedupeTests(unittest.TestCase):
    def test_explicit_existing_default_wins_over_last_used_table(self):
        configured = make_library_target(
            name="长期默认表",
            url="https://example.feishu.cn/base/APP1?table=TBL1",
        )
        recent = make_library_target(
            name="刚刚使用的表",
            url="https://example.feishu.cn/base/APP2?table=TBL2",
        )

        selected = preferred_library_target(
            [configured, recent],
            {
                "existing_single_target_id": configured["target_id"],
                "single_target_id": recent["target_id"],
                "recent_target_id": recent["target_id"],
            },
            context="single",
        )

        self.assertEqual(selected["target_id"], configured["target_id"])

    def test_mapping_omits_invalid_values_and_filters_to_target_fields(self):
        record = WorkArchiveRecord(
            title="作品",
            like_count=True,
            collect_count=-1,
            comment_count=0,
            work_url="https://example.com/work/1",
            cover_url="file:///tmp/cover.jpg",
        )
        fields = record_to_bitable_fields(
            record,
            allowed_fields={"标题", "点赞", "收藏", "评论", "封面链接"},
        )
        self.assertEqual(fields, {"标题": "作品", "评论": 0})

    def test_batch_dedupe_keeps_first_identity_and_all_unidentified_records(self):
        records = [
            WorkArchiveRecord(platform="douyin", work_id="1", title="首条"),
            WorkArchiveRecord(platform="douyin", work_id="1", title="重复"),
            WorkArchiveRecord(
                work_url="https://example.com/item/2?utm_source=a",
                title="URL 首条",
            ),
            WorkArchiveRecord(
                work_url="https://EXAMPLE.com/item/2/#detail",
                title="URL 重复",
            ),
            WorkArchiveRecord(title="无身份一"),
            WorkArchiveRecord(title="无身份二"),
        ]
        result = dedupe_archive_records(records)
        self.assertEqual(
            [record.title for record in result.records],
            ["首条", "URL 首条", "无身份一", "无身份二"],
        )
        self.assertEqual(result.duplicate_count, 2)
        self.assertEqual(
            [(item.first_index, item.duplicate_index) for item in result.duplicates],
            [(0, 1), (2, 3)],
        )

    def test_batch_dedupe_rejects_non_archive_values(self):
        with self.assertRaisesRegex(TypeError, "WorkArchiveRecord"):
            dedupe_archive_records([WorkArchiveRecord(title="ok"), object()])

    def test_batch_dedupe_skips_keys_already_present_in_target_table(self):
        records = [
            WorkArchiveRecord(platform="douyin", work_id="1", title="已存在"),
            WorkArchiveRecord(platform="douyin", work_id="1", title="同批重复"),
            WorkArchiveRecord(platform="douyin", work_id="2", title="新增"),
        ]

        result = dedupe_archive_records(
            records,
            existing_work_keys={"douyin:1"},
        )

        self.assertEqual([record.title for record in result.records], ["新增"])
        self.assertEqual(result.source_indexes, (2,))
        self.assertEqual(result.existing_duplicate_count, 2)
        self.assertEqual(result.duplicate_count, 2)

    def test_standard_table_dedupe_uses_normalized_original_urls(self):
        records = [
            WorkArchiveRecord(
                platform="douyin",
                work_id="1",
                work_url="https://www.douyin.com/video/1?utm_source=lulu#detail",
                title="已存在",
            ),
            WorkArchiveRecord(
                platform="douyin",
                work_id="2",
                work_url="https://www.douyin.com/video/2",
                title="新增",
            ),
        ]

        result = dedupe_archive_records(
            records,
            existing_work_urls={"https://www.douyin.com/video/1"},
        )

        self.assertEqual([record.title for record in result.records], ["新增"])
        self.assertEqual(result.source_indexes, (1,))
        self.assertEqual(result.existing_duplicate_count, 1)

    def test_target_identity_uses_resolved_table_not_link_variant(self):
        base = make_library_target(
            name="Base 链接",
            url="https://example.feishu.cn/base/APP1?table=TBL1&view=VIEW1",
            app_token="APP1",
            table_id="TBL1",
        )
        wiki = make_library_target(
            name="Wiki 链接",
            url="https://example.feishu.cn/wiki/NODE1?table=TBL1&view=VIEW2",
            app_token="APP1",
            table_id="TBL1",
        )
        self.assertEqual(base["target_id"], wiki["target_id"])
        merged = upsert_library_target([base], wiki)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["name"], "Wiki 链接")


if __name__ == "__main__":
    unittest.main()
