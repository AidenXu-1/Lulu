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


if __name__ == "__main__":
    unittest.main()
