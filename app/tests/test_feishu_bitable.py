"""飞书多维表格导出单测：所有 HTTP、凭据和文件均为离线 mock。"""

import hashlib
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

assert sys.version_info[:3] == (3, 11, 15)
assert Path(sys.executable).resolve() == (
    Path(__file__).resolve().parents[2] / ".venv/bin/python"
).resolve()
assert sys.dont_write_bytecode

_ORIGINAL_SOCKET_HAS_IPV6 = socket.has_ipv6
with patch.object(socket, "has_ipv6", False):
    import requests as _PREIMPORTED_REQUESTS
assert socket.has_ipv6 == _ORIGINAL_SOCKET_HAS_IPV6

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.collect.models import CollectedItem
from core.collect import bitable_media, export
from core import feishu, models_manager


def _test_wav_bytes(marker: int) -> bytes:
    pcm = bytes((marker, 0)) * 4
    return (
        b"RIFF"
        + (36 + len(pcm)).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + b"\x01\x00\x01\x00\x80\xbb\x00\x00\x00w\x01\x00\x02\x00\x10\x00"
        + b"data"
        + len(pcm).to_bytes(4, "little")
        + pcm
    )


def _managed_media_item(root: Path, work_id: str, payload: bytes) -> CollectedItem:
    stable_key = f"douyin:{work_id}"
    digest = hashlib.sha256(payload).hexdigest()
    identity = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:24]
    relative = f"douyin/{identity}-{digest[:20]}.wav"
    (root / "douyin").mkdir(parents=True, exist_ok=True)
    (root / relative).write_bytes(payload)
    return CollectedItem(
        work_id,
        title=f"作品 {work_id}",
        media_status="downloaded",
        media_local_path=relative,
    )


class _FakeMediaResponse:
    status_code = 200
    headers: dict = {}

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class TestBitableRecords(unittest.TestCase):
    def _item(self, **kw):
        base = dict(aweme_id="a1", author_name="小泽", title="标题x",
                    create_time=1781416800, duration_ms=87000,
                    like=1321, collect=248, comment=23, share=50,
                    work_url="https://x/work/a1",
                    play_url="https://x/v.mp4", aweme_type=0)
        base.update(kw)
        return CollectedItem(**base)

    def test_fields_types_and_values(self):
        recs = export.to_bitable_records([self._item()], {"a1": "逐字稿"})
        f = recs[0]["fields"]
        self.assertEqual(f["作者昵称"], "小泽")
        self.assertEqual(f["标题"], "标题x")
        self.assertEqual(f["发布日期"], 1781416800 * 1000)        # epoch 毫秒
        self.assertEqual(f["点赞"], 1321)
        for name in ("点赞", "收藏", "评论", "分享"):
            self.assertIs(type(f[name]), int)
        self.assertNotIn("封面", f)
        self.assertEqual(
            f["原作品链接"],
            {"link": "https://x/work/a1", "text": "https://x/work/a1"},
        )
        self.assertEqual(
            f["音视频直链"],
            {"link": "https://x/v.mp4", "text": "https://x/v.mp4"},
        )
        self.assertEqual(f["转录文稿"], "逐字稿")

    def test_omits_empty_and_invalid(self):
        # 无作者、无 create_time、无 play_url、无转录 → 这些字段都不出现
        it = self._item(
            author_name="",
            create_time=0,
            work_url="",
            play_url="",
        )
        f = export.to_bitable_records([it], {})[0]["fields"]
        self.assertNotIn("作者昵称", f)
        self.assertNotIn("发布日期", f)
        self.assertNotIn("原作品链接", f)
        self.assertNotIn("音视频直链", f)
        self.assertNotIn("转录文稿", f)
        self.assertIn("点赞", f)        # 数字仍在

        invalid_stats = self._item(like=True, collect=-1)
        invalid_fields = export.to_bitable_records([invalid_stats])[0]["fields"]
        self.assertNotIn("点赞", invalid_fields)
        self.assertNotIn("收藏", invalid_fields)

    def test_field_schema_matches_headers(self):
        payloads = [feishu._bitable_field_payload(field) for field in export.BITABLE_FIELDS]
        names = [field["field_name"] for field in payloads]
        self.assertEqual(names, export.BITABLE_HEADERS)
        self.assertEqual(len(payloads), 13)
        by_name = {field["field_name"]: field for field in payloads}
        for name in ("点赞", "收藏", "评论", "分享"):
            self.assertEqual(by_name[name]["type"], feishu.BT_NUMBER)
            self.assertEqual(by_name[name]["property"], {"formatter": "0"})
        self.assertEqual(by_name["发布日期"]["type"], feishu.BT_DATETIME)
        self.assertEqual(by_name["原作品链接"]["type"], feishu.BT_URL)
        self.assertEqual(by_name["音视频直链"]["type"], feishu.BT_URL)
        self.assertEqual(by_name["封面"], {
            "field_name": "封面",
            "type": feishu.BT_ATTACHMENT,
            "property": None,
        })
        self.assertEqual(by_name["音视频附件"], {
            "field_name": "音视频附件",
            "type": feishu.BT_ATTACHMENT,
            "property": None,
        })
        self.assertIn("bitable:app", feishu.SCOPES.split())
        self.assertNotIn("docs:document.media:upload", feishu.SCOPES.split())

    def test_link_fields_never_backfill_each_other(self):
        both = self._item()
        work_only = CollectedItem(
            "",
            platform="xiaohongshu",
            work_id="x-work",
            work_url="https://www.xiaohongshu.com/explore/x-work",
            risk_status="rate_limited",
        )
        media_only = CollectedItem(
            "",
            platform="bilibili",
            work_id="b-media",
            play_url="https://cdn.example/b-media.mp4",
        )
        neither = CollectedItem(
            "",
            platform="xiaohongshu",
            work_id="x-empty",
        )
        fields = [
            row["fields"]
            for row in export.to_bitable_records(
                [both, work_only, media_only, neither]
            )
        ]
        self.assertIn("原作品链接", fields[0])
        self.assertIn("音视频直链", fields[0])
        self.assertIn("原作品链接", fields[1])
        self.assertNotIn("音视频直链", fields[1])
        self.assertNotIn("原作品链接", fields[2])
        self.assertIn("音视频直链", fields[2])
        self.assertNotIn("原作品链接", fields[3])
        self.assertNotIn("音视频直链", fields[3])

    def test_transcript_lookup_prefers_stable_key_but_accepts_legacy_douyin_id(self):
        stable = export.to_bitable_records(
            [self._item()],
            {"douyin:a1": "[00:00] 稳定键文稿", "a1": "旧映射"},
        )[0]["fields"]
        legacy = export.to_bitable_records(
            [self._item()],
            {"a1": "[00:00] 旧版文稿"},
        )[0]["fields"]
        self.assertEqual(stable["转录文稿"], "[00:00] 稳定键文稿")
        self.assertEqual(legacy["转录文稿"], "[00:00] 旧版文稿")


class TestBitableCoverPreflight(unittest.TestCase):
    def _item(self, work_id: str, relative: str, *, status: str = "downloaded"):
        return CollectedItem(
            "",
            platform="xiaohongshu",
            work_id=work_id,
            title=f"作品 {work_id}",
            cover_url=f"https://signed.example.test/{work_id}?secret=query",
            cover_status=status,
            cover_local_path=relative,
        )

    def test_only_stable_local_jpeg_png_webp_become_upload_candidates(self):
        with tempfile.TemporaryDirectory() as raw:
            data_root = Path(raw) / "app-data"
            cover_root = data_root / "collection" / "covers" / "xiaohongshu"
            cover_root.mkdir(parents=True)
            valid = {
                "good.jpg": b"\xff\xd8\xffjpeg",
                "good.png": b"\x89PNG\r\n\x1a\npn",
                "good.webp": b"RIFF\x00\x00\x00\x00WEBP",
            }
            for name, payload in valid.items():
                (cover_root / name).write_bytes(payload)
            (cover_root / "empty.png").write_bytes(b"")
            (cover_root / "oversize.png").write_bytes(
                b"\x89PNG\r\n\x1a\n" + b"x" * 9
            )
            (cover_root / "mismatch.jpg").write_bytes(b"\x89PNG\r\n\x1a\n")
            outside = Path(raw) / "outside.png"
            outside.write_bytes(valid["good.png"])
            os.symlink(outside, cover_root / "symlink.png")
            hard_source = cover_root / "hard-source.png"
            hard_source.write_bytes(valid["good.png"])
            os.link(hard_source, cover_root / "hard.png")

            items = [
                self._item("jpg", "xiaohongshu/good.jpg"),
                self._item("png", "xiaohongshu/good.png"),
                self._item("webp", "xiaohongshu/good.webp"),
                self._item("no-path", ""),
                self._item("missing", "xiaohongshu/missing.png"),
                self._item("empty", "xiaohongshu/empty.png"),
                self._item("oversize", "xiaohongshu/oversize.png"),
                self._item("symlink", "xiaohongshu/symlink.png"),
                self._item("hard", "xiaohongshu/hard.png"),
                self._item("mismatch", "xiaohongshu/mismatch.jpg"),
                self._item("url-only", "", status="url_only"),
            ]
            with (
                patch("core.models_manager.app_data_dir", return_value=data_root),
                patch.object(export, "_BITABLE_COVER_MAX_BYTES", 16),
            ):
                preflight = export.prepare_bitable_export(items)

        self.assertEqual(preflight.row_count, len(items))
        self.assertEqual(len(preflight.cover_candidates), 3)
        self.assertEqual(len(preflight.cover_skipped), 8)
        self.assertEqual(
            {candidate.mime_type for candidate in preflight.cover_candidates},
            {"image/jpeg", "image/png", "image/webp"},
        )
        for candidate in preflight.cover_candidates:
            self.assertTrue(candidate.file_name.startswith("cover-"))
            self.assertNotIn("/", candidate.file_name)
            self.assertNotIn("\\", candidate.file_name)
        rendered_issues = "\n".join(
            f"{issue.stable_key}:{issue.reason_code}:{issue.retry_hint}"
            for issue in preflight.cover_skipped
        )
        self.assertNotIn(str(data_root), rendered_issues)
        self.assertNotIn("signed.example.test", rendered_issues)
        for record in preflight.records:
            self.assertNotIn("封面", record["fields"])

        self.assertIn(
            "cover_not_downloaded",
            {issue.reason_code for issue in preflight.cover_skipped},
        )


class TestBitableMediaP1Regressions(unittest.TestCase):
    def test_requests_preimport_is_offline_and_restores_socket_capability(self):
        self.assertIs(sys.modules["requests"], _PREIMPORTED_REQUESTS)
        self.assertEqual(socket.has_ipv6, _ORIGINAL_SOCKET_HAS_IPV6)

    def test_managed_media_path_escapes_are_uniformly_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "media"
            (root / "douyin").mkdir(parents=True)
            invalid_paths = (
                "../outside.wav",
                "/absolute.wav",
                "douyin//outside.wav",
                "douyin/../outside.wav",
                "./outside.wav",
                "douyin\\outside.wav",
            )
            for index, relative in enumerate(invalid_paths):
                with self.subTest(kind="lexical", relative=relative):
                    item = CollectedItem(
                        f"lexical-{index}",
                        title="越界素材",
                        media_status="downloaded",
                        media_local_path=relative,
                    )
                    candidates, issues = bitable_media.prepare_bitable_media(
                        (item,), root=root
                    )
                    self.assertEqual(candidates, ())
                    self.assertEqual(
                        tuple(issue.reason_code for issue in issues),
                        ("media_file_rejected",),
                    )

            outside = base / "outside"
            (outside / "douyin").mkdir(parents=True)
            link_cases = []

            file_root = base / "file-root"
            (file_root / "douyin").mkdir(parents=True)
            os.symlink(outside / "outside.wav", file_root / "douyin" / "linked.wav")
            link_cases.append(("file", file_root, "douyin/linked.wav"))

            platform_root = base / "platform-root"
            platform_root.mkdir()
            os.symlink(outside / "douyin", platform_root / "douyin")
            link_cases.append(("platform", platform_root, "douyin/outside.wav"))

            real_root = base / "real-root"
            (real_root / "douyin").mkdir(parents=True)
            linked_root = base / "linked-root"
            os.symlink(real_root, linked_root)
            link_cases.append(("root", linked_root, "douyin/outside.wav"))

            real_parent = base / "real-parent"
            (real_parent / "media" / "douyin").mkdir(parents=True)
            linked_parent = base / "linked-parent"
            os.symlink(real_parent, linked_parent)
            link_cases.append(
                ("ancestor", linked_parent / "media", "douyin/outside.wav")
            )

            for kind, attacked_root, relative in link_cases:
                with self.subTest(kind=kind):
                    item = CollectedItem(
                        f"link-{kind}",
                        title="换链素材",
                        media_status="downloaded",
                        media_local_path=relative,
                    )
                    candidates, issues = bitable_media.prepare_bitable_media(
                        (item,), root=attacked_root
                    )
                    self.assertEqual(candidates, ())
                    self.assertEqual(
                        tuple(issue.reason_code for issue in issues),
                        ("media_file_rejected",),
                    )

    def test_valid_media_enters_fake_http_but_inplace_restore_never_attaches(self):
        with tempfile.TemporaryDirectory() as raw:
            media_root = Path(raw) / "media"
            original = _test_wav_bytes(1)
            changed = _test_wav_bytes(2)
            item = _managed_media_item(media_root, "b1-valid", original)
            preflight = export.prepare_bitable_export([item], media_root=media_root)
            self.assertEqual(len(preflight.media_candidates), 1)
            self.assertEqual(preflight.media_skipped, ())
            candidate = preflight.media_candidates[0]
            source = media_root / item.media_local_path
            captured: dict[str, str] = {}

            def fake_http(url, **kwargs):
                if url.endswith("upload_prepare"):
                    return _FakeMediaResponse({
                        "code": 0,
                        "data": {
                            "upload_id": "MOCK_UPLOAD_ID",
                            "block_size": feishu._BITABLE_FILE_BLOCK_SIZE,
                            "block_num": 1,
                        },
                    })
                if url.endswith("upload_finish"):
                    return _FakeMediaResponse(
                        {"code": 0, "data": {"file_token": "MOCK_FILE_TOKEN"}}
                    )
                before = source.stat()
                source.write_bytes(changed)
                os.utime(source, ns=(before.st_atime_ns, candidate.mtime_ns))
                sent = kwargs["files"]["file"][1]
                captured["sent_sha"] = hashlib.sha256(sent).hexdigest()
                source.write_bytes(original)
                os.utime(source, ns=(before.st_atime_ns, candidate.mtime_ns))
                return _FakeMediaResponse({"code": 0, "data": {}})

            with (
                patch.object(feishu, "_user_token", return_value="MOCK_USER_TOKEN"),
                patch.object(feishu, "create_wiki_bitable", return_value={
                    "app_token": "APP1",
                    "node_token": "NODE1",
                    "url": "https://feishu.cn/wiki/NODE1",
                }),
                patch.object(
                    feishu, "_prepare_initial_bitable_table", return_value="TBL1"
                ),
                patch.object(
                    feishu,
                    "ensure_bitable_media_field",
                    return_value=feishu.BitableMediaFieldReceipt("ready"),
                ),
                patch.object(
                    feishu,
                    "batch_create_records",
                    return_value=feishu.BitableBatchReceipt(
                        1,
                        1,
                        0,
                        record_id_by_index=((0, "REC1"),),
                    ),
                ),
                patch("requests.post", side_effect=fake_http) as post,
                patch.object(feishu, "_api", return_value={}) as attach_api,
                patch.object(feishu, "_BITABLE_MEDIA_MIN_INTERVAL", 0),
                patch.object(
                    models_manager,
                    "app_data_dir",
                    return_value=Path(raw) / "app-data",
                ),
            ):
                receipt = feishu.export_collection_to_new_bitable(
                    "SPACE",
                    "PARENT",
                    "标题",
                    "采集结果",
                    export.BITABLE_FIELDS,
                    preflight.records,
                    preflight.cover_candidates,
                    preflight.cover_skipped,
                    preflight.media_candidates,
                    preflight.media_skipped,
                )

            frozen_sha = candidate.sha256
            final_sha = hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertEqual(post.call_count, 3)
            self.assertEqual(captured["sent_sha"], frozen_sha)
            self.assertEqual(final_sha, frozen_sha)
            self.assertEqual((receipt.media_success, receipt.media_failed), (0, 1))
            self.assertIn("media_file_changed", {issue.reason_code for issue in receipt.issues})
            attach_api.assert_not_called()

    def test_receipt_root_and_ancestor_rechain_are_fail_closed(self):
        intent_hash = "a" * 64
        for attack_level in ("root", "ancestor"):
            with self.subTest(attack_level=attack_level), tempfile.TemporaryDirectory() as raw:
                base = Path(raw) / "base"
                receipt_root = base / "parent" / "receipts"
                outside = base / "outside"
                outside.mkdir(parents=True)
                attacked = receipt_root if attack_level == "root" else receipt_root.parent
                if attack_level == "ancestor":
                    (outside / "receipts").mkdir()
                detached = base / f"detached-{attack_level}"
                real_replace = os.replace

                def replace_after_rechain(
                    source_name,
                    target_name,
                    *,
                    src_dir_fd=None,
                    dst_dir_fd=None,
                ):
                    attacked.rename(detached)
                    os.symlink(outside, attacked, target_is_directory=True)
                    return real_replace(
                        source_name,
                        target_name,
                        src_dir_fd=src_dir_fd,
                        dst_dir_fd=dst_dir_fd,
                    )

                try:
                    with (
                        patch.object(
                            feishu.os,
                            "replace",
                            side_effect=replace_after_rechain,
                        ) as replace,
                        self.assertRaisesRegex(OSError, "目录发生变化"),
                    ):
                        feishu.BitableMediaReceiptStore(receipt_root).write(
                            intent_hash,
                            {"status": "uploading_parts"},
                        )
                    self.assertEqual(replace.call_count, 1)
                    self.assertEqual(list(outside.rglob("*.json")), [])
                    self.assertEqual(list(detached.rglob("*.json")), [])
                finally:
                    if attacked.is_symlink():
                        attacked.unlink()
                    if detached.exists():
                        detached.rename(attacked)


class TestBitableApiAssembly(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.initial_records = [
            {"record_id": f"RECEMPTY{index}", "fields": {}}
            for index in range(10)
        ]
        self._orig_api = feishu._api
        self._orig_token = feishu._user_token
        feishu._user_token = lambda: "tok"

        def fake_api(method, path, token, timeout=30, **kw):
            self.calls.append((method, path, kw))
            if path.startswith("/wiki/") and method == "POST":
                return {"node": {"obj_token": "APP1", "node_token": "NODE1"}}
            if path.endswith("/tables") and method == "POST":
                return {"table_id": "TBLNEW"}
            if path.endswith("/tables?page_size=100") or (path.endswith("/tables") and method == "GET"):
                return {"items": [{"table_id": "TBLDEFAULT"}]}
            if path.endswith("/fields?page_size=100") and method == "GET":
                return {
                    "items": [
                        {"field_id": "FLDDEFAULT", "field_name": "多行文本", "type": 1}
                    ]
                }
            if "/fields?client_token=" in path and method == "POST":
                return {"field": {"field_id": f"F{len(self.calls)}"}}
            if path.endswith("/records?page_size=500") and method == "GET":
                return {"items": list(self.initial_records), "has_more": False}
            if path.endswith("/records/batch_delete") and method == "POST":
                return {}
            if "batch_create" in path:
                return {"records": []}
            return {}

        feishu._api = fake_api

    def tearDown(self):
        feishu._api = self._orig_api
        feishu._user_token = self._orig_token

    def test_export_to_new_bitable_flow(self):
        records = [{"fields": {"标题": "t"}}]
        url = feishu.export_to_new_bitable(
            "SP1", "PARENT", "我的表", "采集结果", export.BITABLE_FIELDS, records)
        paths = [(m, p) for m, p, _ in self.calls]
        # 1) 知识库下建 bitable 节点
        self.assertIn(("POST", "/wiki/v2/spaces/SP1/nodes"), paths)
        node_call = next(kw for m, p, kw in self.calls if p == "/wiki/v2/spaces/SP1/nodes")
        self.assertEqual(node_call["json"]["obj_type"], "bitable")
        self.assertEqual(node_call["json"]["parent_node_token"], "PARENT")
        # 2) 复用并重命名飞书自带的唯一空白表
        self.assertIn(("PATCH", "/bitable/v1/apps/APP1/tables/TBLDEFAULT"), paths)
        rename_call = next(
            kw for m, p, kw in self.calls
            if p == "/bitable/v1/apps/APP1/tables/TBLDEFAULT" and m == "PATCH"
        )
        self.assertEqual(rename_call["json"], {"name": "采集结果"})
        self.assertNotIn(("POST", "/bitable/v1/apps/APP1/tables"), paths)
        self.assertNotIn(("DELETE", "/bitable/v1/apps/APP1/tables/TBLDEFAULT"), paths)
        # 3) 就地更新主字段，再创建其余标准字段
        self.assertIn(
            ("PUT", "/bitable/v1/apps/APP1/tables/TBLDEFAULT/fields/FLDDEFAULT"),
            paths,
        )
        field_creates = [
            kw for method, path, kw in self.calls
            if method == "POST" and "/tables/TBLDEFAULT/fields?client_token=" in path
        ]
        self.assertEqual(len(field_creates), len(export.BITABLE_FIELDS) - 1)
        # 4) 正式写入前精确移除飞书自带的 10 条全空初始记录
        blank_read_index = next(
            index for index, (_method, path, _kw) in enumerate(self.calls)
            if path.endswith("/records?page_size=500")
        )
        blank_delete_index = next(
            index for index, (_method, path, _kw) in enumerate(self.calls)
            if path.endswith("/records/batch_delete")
        )
        row_create_index = next(
            index for index, (_method, path, _kw) in enumerate(self.calls)
            if "/records/batch_create" in path
        )
        self.assertLess(blank_read_index, blank_delete_index)
        self.assertLess(blank_delete_index, row_create_index)
        delete_call = self.calls[blank_delete_index][2]
        self.assertEqual(
            delete_call["json"],
            {"records": [f"RECEMPTY{index}" for index in range(10)]},
        )
        # 5) 批量写入同一张复用表
        self.assertTrue(any("/tables/TBLDEFAULT/records/batch_create" in p for _, p, _ in self.calls))
        # 6) 返回 wiki 链接
        self.assertEqual(url, "https://feishu.cn/wiki/NODE1")

    def test_initial_table_with_any_real_value_fails_closed_before_delete(self):
        self.initial_records = [
            {"record_id": "RECREAL", "fields": {"多行文本": "已有内容"}}
        ]

        with self.assertRaisesRegex(RuntimeError, "初始记录不是全空"):
            feishu._prepare_initial_bitable_table(
                "APP1",
                "采集结果",
                export.BITABLE_FIELDS,
                "tok",
            )

        self.assertFalse(
            any(path.endswith("/records/batch_delete") for _method, path, _kw in self.calls)
        )

    def test_batch_chunks_over_limit(self):
        recs = [{"fields": {}} for _ in range(feishu._BT_BATCH + 7)]
        receipt = feishu.batch_create_records("APP1", "TBLNEW", recs)
        self.assertTrue(receipt.complete)
        self.assertEqual(receipt.succeeded, len(recs))
        self.assertEqual(receipt.failed, 0)
        batch_calls = [c for c in self.calls if "batch_create" in c[1]]
        self.assertEqual(len(batch_calls), 2)   # 分两批

    def test_single_failed_record_returns_partial_receipt_and_no_false_success(self):
        original_fake = feishu._api

        def fake_with_one_failure(method, path, token, timeout=30, **kw):
            if "batch_create" in path:
                title = kw["json"]["records"][0]["fields"]["标题"]
                if title == "失败项":
                    raise RuntimeError("fixture single failure")
            return original_fake(method, path, token, timeout=timeout, **kw)

        feishu._api = fake_with_one_failure
        records = [
            {"fields": {"标题": "成功一"}},
            {"fields": {"标题": "失败项"}},
            {"fields": {"标题": "成功二"}},
        ]
        with patch.object(feishu, "_BT_BATCH", 1):
            receipt = feishu.batch_create_records("APP1", "TBLNEW", records)
            self.assertEqual(
                (receipt.attempted, receipt.succeeded, receipt.failed),
                (3, 2, 1),
            )
            self.assertEqual(receipt.failed_indexes, (1,))
            with self.assertRaises(feishu.BitablePartialWriteError) as raised:
                feishu.export_to_new_bitable(
                    "SP1",
                    "PARENT",
                    "我的表",
                    "采集结果",
                    export.BITABLE_FIELDS,
                    records,
                )
        self.assertEqual(raised.exception.receipt.succeeded, 2)
        self.assertEqual(raised.exception.receipt.failed, 1)

    def test_attachment_multipart_exact_endpoint_parent_scope_and_payload(self):
        class Response:
            def json(self):
                return {"code": 0, "data": {"file_token": "MOCK_FILE_TOKEN"}}

        png = b"\x89PNG\r\n\x1a\npayload"
        with patch("requests.post", return_value=Response()) as post:
            receipt = feishu.upload_bitable_image(
                "APP1",
                "cover-deadbeef.png",
                "image/png",
                png,
                token="MOCK_USER_TOKEN",
            )

        self.assertEqual(receipt.status, "uploaded")
        self.assertEqual(receipt.attempts, 1)
        self.assertNotIn("MOCK_FILE_TOKEN", repr(receipt))
        args, kwargs = post.call_args
        self.assertEqual(
            args[0],
            "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all",
        )
        self.assertEqual(
            set(kwargs["files"]),
            {"file_name", "parent_type", "parent_node", "size", "extra", "file"},
        )
        self.assertEqual(kwargs["files"]["file_name"], (None, "cover-deadbeef.png"))
        self.assertEqual(kwargs["files"]["parent_type"], (None, "bitable_image"))
        self.assertEqual(kwargs["files"]["parent_node"], (None, "APP1"))
        self.assertEqual(kwargs["files"]["size"], (None, str(len(png))))
        self.assertEqual(
            json.loads(kwargs["files"]["extra"][1]),
            {"drive_route_token": "APP1"},
        )
        self.assertEqual(kwargs["files"]["file"], (
            "cover-deadbeef.png",
            png,
            "image/png",
        ))

    def test_attachment_1061045_retries_once_but_read_timeout_is_uncertain(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        png = b"\x89PNG\r\n\x1a\npayload"
        responses = [
            Response({"code": 1061045, "msg": "retry"}),
            Response({"code": 0, "data": {"file_token": "TOKEN_OK"}}),
        ]
        with (
            patch("requests.post", side_effect=responses) as post,
            patch.object(feishu.time, "sleep"),
        ):
            retried = feishu.upload_bitable_image(
                "APP1", "cover-a.png", "image/png", png, token="MOCK"
            )
        self.assertEqual((retried.status, retried.attempts), ("uploaded", 2))
        self.assertEqual(post.call_count, 2)

        import requests

        with patch(
            "requests.post",
            side_effect=requests.exceptions.ReadTimeout(
                "https://signed.example.test/secret?token=leak"
            ),
        ) as post:
            uncertain = feishu.upload_bitable_image(
                "APP1", "cover-a.png", "image/png", png, token="MOCK"
            )
        self.assertEqual(
            (uncertain.status, uncertain.attempts, post.call_count),
            ("upload_uncertain", 1, 1),
        )
        self.assertNotIn("signed.example.test", repr(uncertain))
        self.assertNotIn("token=leak", repr(uncertain))

    def test_all_feishu_posts_disable_redirects_and_3xx_never_replay(self):
        class Response:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self.payload = payload
                self.json_calls = 0

            def json(self):
                self.json_calls += 1
                return self.payload

        png = b"\x89PNG\r\n\x1a\npayload"
        for status_code in (301, 302, 303, 307, 308):
            with self.subTest(status_code=status_code):
                response = Response(
                    status_code,
                    {"code": 0, "data": {"file_token": "MUST_NOT_BE_USED"}},
                )
                with patch("requests.post", return_value=response) as post:
                    receipt = feishu.upload_bitable_image(
                        "APP1", "cover-a.png", "image/png", png, token="MOCK"
                    )
                self.assertEqual(post.call_count, 1)
                self.assertIs(post.call_args.kwargs["allow_redirects"], False)
                self.assertEqual(response.json_calls, 0)
                self.assertEqual(
                    (receipt.status, receipt.attempts),
                    ("upload_uncertain", 1),
                )
                self.assertNotIn("MUST_NOT_BE_USED", repr(receipt))

        api_response = Response(307, {"code": 0, "data": {"ok": True}})
        with patch("requests.request", return_value=api_response) as request:
            with self.assertRaises(feishu.FeishuTransportError) as raised:
                self._orig_api("POST", "/fixture", "MOCK", json={"x": 1})
        self.assertEqual(request.call_count, 1)
        self.assertIs(request.call_args.kwargs["allow_redirects"], False)
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(api_response.json_calls, 0)

        oauth_response = Response(302, {"access_token": "MUST_NOT_BE_USED"})
        with patch("requests.post", return_value=oauth_response) as post:
            with self.assertRaises(RuntimeError):
                feishu._oauth_token({"grant_type": "fixture"})
        self.assertEqual(post.call_count, 1)
        self.assertIs(post.call_args.kwargs["allow_redirects"], False)
        self.assertEqual(oauth_response.json_calls, 0)

        md_response = Response(
            303,
            {"code": 0, "data": {"file_token": "MUST_NOT_BE_USED"}},
        )
        with patch("requests.post", return_value=md_response) as post:
            with self.assertRaises(RuntimeError):
                feishu._upload_md("MOCK", "fixture.md", "fixture")
        self.assertEqual(post.call_count, 1)
        self.assertIs(post.call_args.kwargs["allow_redirects"], False)
        self.assertEqual(md_response.json_calls, 0)

    def test_record_retry_reuses_client_token_and_update_never_creates(self):
        create_calls = []

        def retry_once(method, path, token, timeout=30, **kw):
            create_calls.append((path, kw["json"]))
            if len(create_calls) == 1:
                raise feishu.FeishuApiError(1061045, "retryable")
            return {"records": [{"record_id": "REC1"}]}

        with (
            patch.object(feishu, "_api", side_effect=retry_once),
            patch.object(feishu, "uuid4", return_value="stable-create-token"),
            patch.object(feishu.time, "sleep"),
        ):
            receipt = feishu.batch_create_records(
                "APP1", "TBL1", [{"fields": {"标题": "一"}}]
            )
        self.assertTrue(receipt.complete)
        self.assertEqual(receipt.record_ids, ("REC1",))
        self.assertEqual(create_calls[0], create_calls[1])
        self.assertIn("client_token=stable-create-token", create_calls[0][0])

        update_calls = []

        def update_api(method, path, token, timeout=30, **kw):
            update_calls.append((path, kw["json"]))
            return {"records": [{"record_id": "REC1"}]}

        update = [{
            "record_id": "REC1",
            "fields": {"封面": [{"file_token": "MOCK_FILE_TOKEN"}]},
        }]
        with (
            patch.object(feishu, "_api", side_effect=update_api),
            patch.object(feishu, "uuid4", return_value="stable-update-token"),
        ):
            updated = feishu.batch_update_records("APP1", "TBL1", update)
        self.assertTrue(updated.complete)
        self.assertEqual(len(update_calls), 1)
        self.assertIn("/records/batch_update?", update_calls[0][0])
        self.assertNotIn("batch_create", update_calls[0][0])
        self.assertEqual(update_calls[0][1]["records"], update)

    def test_record_uncertain_response_is_not_retried(self):
        with patch.object(
            feishu,
            "_api",
            side_effect=feishu.FeishuTransportError(
                retryable_not_sent=False,
                uncertain=True,
            ),
        ) as api:
            receipt = feishu.batch_create_records(
                "APP1", "TBL1", [{"fields": {"标题": "一"}}]
            )
        self.assertEqual(api.call_count, 1)
        self.assertEqual(receipt.unconfirmed_indexes, (0,))
        self.assertFalse(receipt.complete)

    def test_invalid_record_response_is_uncertain_and_does_not_echo_body(self):
        class Response:
            status_code = 502
            text = "MOCK_USER_TOKEN MOCK_FILE_TOKEN /Users/private signed.example.test"

            def json(self):
                raise ValueError("invalid")

        with patch("requests.request", return_value=Response()):
            with self.assertRaises(feishu.FeishuTransportError) as raised:
                self._orig_api(
                    "POST",
                    "/bitable/v1/apps/APP1/tables/TBL1/records/batch_create",
                    "MOCK_USER_TOKEN",
                    json={"records": []},
                )
        self.assertTrue(raised.exception.uncertain)
        rendered = str(raised.exception)
        for secret in (
            "MOCK_USER_TOKEN",
            "MOCK_FILE_TOKEN",
            "/Users/private",
            "signed.example.test",
        ):
            self.assertNotIn(secret, rendered)

    def test_cover_failure_is_partial_serial_and_record_payload_has_only_token(self):
        png = b"\x89PNG\r\n\x1a\npayload"
        candidates = (
            export.BitableCoverCandidate(0, "x:1", "失败作品", "cover-1.png", "image/png", png),
            export.BitableCoverCandidate(1, "x:2", "成功作品", "cover-2.png", "image/png", png),
        )
        preflight_issue = export.BitableCoverIssue(
            2,
            "x:3",
            "缺失作品",
            "cover_file_rejected",
            "检查本地封面后再试",
        )
        active = 0
        max_active = 0
        upload_calls = []

        def upload(_app, file_name, _mime, _data, *, token=None):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            upload_calls.append(file_name)
            active -= 1
            if file_name == "cover-1.png":
                return feishu.BitableCoverUploadReceipt(
                    "final_failed", 1, "cover_upload_rejected"
                )
            return feishu.BitableCoverUploadReceipt(
                "uploaded", 1, file_token="MOCK_FILE_TOKEN_2"
            )

        captured = {}

        def write_records(_app, _table, records, *, is_cancelled=None):
            captured["records"] = records
            return feishu.BitableBatchReceipt(3, 3, 0)

        with (
            patch.object(feishu, "create_wiki_bitable", return_value={
                "app_token": "APP1",
                "node_token": "NODE1",
                "url": "https://feishu.cn/wiki/NODE1",
            }),
            patch.object(
                feishu, "_prepare_initial_bitable_table", return_value="TBL1"
            ),
            patch.object(feishu, "upload_bitable_image", side_effect=upload),
            patch.object(feishu, "batch_create_records", side_effect=write_records),
        ):
            receipt = feishu.export_collection_to_new_bitable(
                "SP1",
                "PARENT",
                "标题",
                "采集结果",
                export.BITABLE_FIELDS,
                [{"fields": {"点赞": 1}}, {"fields": {"点赞": 2}}, {"fields": {}}],
                candidates,
                (preflight_issue,),
            )

        self.assertEqual(upload_calls, ["cover-1.png", "cover-2.png"])
        self.assertEqual(max_active, 1)
        self.assertNotIn("封面", captured["records"][0]["fields"])
        self.assertEqual(
            captured["records"][1]["fields"]["封面"],
            [{"file_token": "MOCK_FILE_TOKEN_2"}],
        )
        self.assertEqual(
            set(captured["records"][1]["fields"]["封面"][0]),
            {"file_token"},
        )
        self.assertEqual(
            (receipt.row_success, receipt.cover_success, receipt.cover_failed, receipt.cover_skipped),
            (3, 1, 1, 1),
        )
        details = receipt.safe_details()
        self.assertNotIn("MOCK_FILE_TOKEN_2", details)
        self.assertNotIn("signed.example.test", details)
        self.assertNotIn("/Users/", details)

    def test_cancel_before_call_is_zero_effect_and_midway_keeps_real_receipt(self):
        pre_cancelled = feishu.export_collection_to_new_bitable(
            "SP1",
            "PARENT",
            "标题",
            "采集结果",
            export.BITABLE_FIELDS,
            [{"fields": {}}, {"fields": {}}],
            is_cancelled=lambda: True,
        )
        self.assertTrue(pre_cancelled.cancelled)
        self.assertEqual(pre_cancelled.row_cancelled, 2)
        self.assertEqual(pre_cancelled.url, "")
        self.assertEqual(self.calls, [])

        png = b"\x89PNG\r\n\x1a\npayload"
        candidates = (
            export.BitableCoverCandidate(0, "x:1", "一", "cover-1.png", "image/png", png),
            export.BitableCoverCandidate(1, "x:2", "二", "cover-2.png", "image/png", png),
        )
        state = {"cancelled": False}

        def upload(*_args, **_kwargs):
            state["cancelled"] = True
            return feishu.BitableCoverUploadReceipt(
                "uploaded", 1, file_token="MOCK_FILE_TOKEN"
            )

        with (
            patch.object(feishu, "create_wiki_bitable", return_value={
                "app_token": "APP1",
                "node_token": "NODE1",
                "url": "https://feishu.cn/wiki/NODE1",
            }),
            patch.object(
                feishu, "_prepare_initial_bitable_table", return_value="TBL1"
            ),
            patch.object(feishu, "upload_bitable_image", side_effect=upload),
            patch.object(feishu, "batch_create_records") as batch,
        ):
            midway = feishu.export_collection_to_new_bitable(
                "SP1",
                "PARENT",
                "标题",
                "采集结果",
                export.BITABLE_FIELDS,
                [{"fields": {}}, {"fields": {}}],
                candidates,
                is_cancelled=lambda: state["cancelled"],
            )
        batch.assert_not_called()
        self.assertTrue(midway.cancelled)
        self.assertEqual(midway.url, "https://feishu.cn/wiki/NODE1")
        self.assertEqual(midway.cover_success, 1)
        self.assertEqual(midway.cover_skipped, 1)
        self.assertEqual(midway.row_cancelled, 2)

    def test_node_and_table_uncertainty_preserve_structured_receipts(self):
        records = [{"fields": {"标题": "一"}}]
        known = {
            "app_token": "APP1",
            "node_token": "NODE1",
            "url": "https://feishu.cn/wiki/NODE1",
        }

        with (
            patch.object(feishu, "_user_token", return_value="MOCK"),
            patch.object(feishu, "create_wiki_bitable", return_value=known),
            patch.object(
                feishu,
                "_list_bitable_tables",
                side_effect=RuntimeError("fixture list failed"),
            ) as table_list,
            patch.object(feishu, "create_bitable_table") as create_table,
            patch.object(feishu, "batch_create_records") as write_records,
        ):
            list_receipt = feishu.export_collection_to_new_bitable(
                "SP1", "PARENT", "标题", "采集结果", export.BITABLE_FIELDS, records
            )
        self.assertEqual(table_list.call_count, 1)
        create_table.assert_not_called()
        write_records.assert_not_called()
        self.assertTrue(list_receipt.outcome_uncertain)
        self.assertEqual(list_receipt.uncertain_stage, "table_prepare")
        self.assertEqual(list_receipt.url, known["url"])
        self.assertEqual(list_receipt.row_unconfirmed, 1)

        with (
            patch.object(feishu, "_user_token", return_value="MOCK"),
            patch.object(feishu, "create_wiki_bitable", return_value=known),
            patch.object(feishu, "_list_bitable_tables", return_value=[]),
            patch.object(
                feishu,
                "_prepare_initial_bitable_table",
                side_effect=feishu.FeishuTransportError(
                    retryable_not_sent=False,
                    uncertain=True,
                ),
            ) as prepare_table,
        ):
            table_receipt = feishu.export_collection_to_new_bitable(
                "SP1", "PARENT", "标题", "采集结果", export.BITABLE_FIELDS, records
            )
        self.assertEqual(prepare_table.call_count, 1)
        self.assertTrue(table_receipt.outcome_uncertain)
        self.assertEqual(table_receipt.uncertain_stage, "table_prepare")
        self.assertEqual(table_receipt.url, known["url"])
        self.assertEqual(table_receipt.row_unconfirmed, 1)

        with (
            patch.object(feishu, "_user_token", return_value="MOCK"),
            patch.object(
                feishu,
                "create_wiki_bitable",
                side_effect=feishu.FeishuTransportError(
                    retryable_not_sent=False,
                    uncertain=True,
                ),
            ) as create_node,
            patch.object(feishu, "_list_bitable_tables") as table_list,
        ):
            node_receipt = feishu.export_collection_to_new_bitable(
                "SP1", "PARENT", "标题", "采集结果", export.BITABLE_FIELDS, records
            )
        self.assertEqual(create_node.call_count, 1)
        table_list.assert_not_called()
        self.assertTrue(node_receipt.outcome_uncertain)
        self.assertEqual(node_receipt.uncertain_stage, "wiki_node_create")
        self.assertEqual(node_receipt.url, "")
        self.assertEqual(node_receipt.row_unconfirmed, 1)

    def test_six_human_summary_fixtures_are_exact_and_redacted(self):
        base = dict(
            url="",
            row_total=1,
            row_success=0,
            row_failed=0,
            row_unconfirmed=0,
            row_cancelled=0,
            cover_candidate=0,
            cover_success=0,
            cover_skipped=0,
            cover_failed=0,
            cover_uncertain=0,
            cancelled=False,
        )
        fixtures = (
            (
                {**base, "url": "https://feishu.cn/wiki/NODE", "outcome_uncertain": True},
                ("飞书结果待确认", "飞书可能已经创建了结果，Lulu 没拿到完整确认", "先打开现有结果确认，不要立即重试"),
            ),
            (
                {**base, "outcome_uncertain": True},
                ("飞书结果待确认", "飞书可能已经创建了结果，但 Lulu 没拿到可打开地址", "到目标知识库按本次标题和时间确认"),
            ),
            (
                {**base, "cover_uncertain": 1},
                ("封面上传待确认", "这张封面可能已经上传，为避免重复附件已停止自动重传", "先查看现有表格的对应作品，再决定是否重新导出"),
            ),
            (
                {**base, "row_unconfirmed": 3},
                ("部分记录待确认", "有 3 行可能已写入，当前无法确认", "先核对表内行数和标题，再处理缺失项"),
            ),
            (
                {**base, "row_failed": 1},
                ("该项未发送成功", "该项没有发送成功", "按标题定位作品，修复本地文件或权限后重新导出"),
            ),
            (
                {**base, "cancelled": True, "row_cancelled": 1},
                ("已停止后续上传", "已经创建或写入的内容会保留", "打开现有结果确认，无需等待自动回滚"),
            ),
        )
        for fields, expected in fixtures:
            with self.subTest(expected=expected[0]):
                receipt = feishu.BitableExportReceipt(**fields)
                summary = receipt.human_summary()
                self.assertEqual(summary, expected)
                joined = " ".join(summary)
                for secret in ("reason_code", "TOKEN", "/Users/", "response_body"):
                    self.assertNotIn(secret, joined)


if __name__ == "__main__":
    unittest.main()
