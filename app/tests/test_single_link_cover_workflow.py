"""P3-C C2 单链接可信身份、转录旁路与素材库 meta 回归。"""

from __future__ import annotations

import json
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
import urllib.request
import webbrowser
from unittest.mock import patch

from PySide6.QtGui import QDesktopServices

from src.core import library, pipeline
from src.core.audio import extractor
from src.core.collect.cover import CoverReceipt
from src.core.collect.preferences import CollectCoverPreferenceSnapshot
from src.core.models import Transcript, TranscriptCover
from src.ui import worker as worker_module


class _Engine:
    display_name = "Fake"

    def __init__(self, text: str = "逐字稿成功") -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, _wav_path, _progress):
        self.calls += 1
        return Transcript(self.text, engine="fake")


class _Workflow:
    def __init__(self, receipt: CoverReceipt | None = None) -> None:
        self.receipt = receipt or CoverReceipt.failed("cover_io_failed")
        self.calls: list[dict] = []

    def localize(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.receipt


class _Preference:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.calls = 0

    def snapshot(self) -> CollectCoverPreferenceSnapshot:
        self.calls += 1
        return CollectCoverPreferenceSnapshot(self.enabled)


def _success_receipt(platform: str = "youtube") -> CoverReceipt:
    return CoverReceipt(
        status="downloaded",
        relative_path=f"{platform}/0123456789abcdef0123456789abcdef.jpg",
        size=128,
        sha256="a" * 64,
        mime_type="image/jpeg",
    )


class SingleLinkCoverWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.external_calls: list[str] = []

        def forbid(name: str):
            def blocked(*_args, **_kwargs):
                cls.external_calls.append(name)
                raise AssertionError(f"禁止真实外部触达: {name}")

            return blocked

        cls._external_guard_patchers = (
            patch.object(
                socket, "getaddrinfo", side_effect=forbid("dns.getaddrinfo")
            ),
            patch.object(socket, "socket", side_effect=forbid("socket.socket")),
            patch.object(
                socket,
                "create_connection",
                side_effect=forbid("socket.create_connection"),
            ),
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=forbid("urllib.request.urlopen"),
            ),
            patch.object(
                urllib.request.OpenerDirector,
                "open",
                side_effect=forbid("urllib.OpenerDirector.open"),
            ),
            patch.object(
                webbrowser, "open", side_effect=forbid("webbrowser.open")
            ),
            patch.object(
                webbrowser,
                "open_new",
                side_effect=forbid("webbrowser.open_new"),
            ),
            patch.object(
                webbrowser,
                "open_new_tab",
                side_effect=forbid("webbrowser.open_new_tab"),
            ),
            patch.object(
                QDesktopServices,
                "openUrl",
                side_effect=forbid("QDesktopServices.openUrl"),
            ),
        )
        for patcher in cls._external_guard_patchers:
            patcher.start()

    @classmethod
    def tearDownClass(cls) -> None:
        for patcher in reversed(cls._external_guard_patchers):
            patcher.stop()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_library_dir = library.LIBRARY_DIR
        self.old_folders_file = library.FOLDERS_FILE
        library.LIBRARY_DIR = self.root / "library"
        library.FOLDERS_FILE = library.LIBRARY_DIR / "folders.json"

    def tearDown(self) -> None:
        library.LIBRARY_DIR = self.old_library_dir
        library.FOLDERS_FILE = self.old_folders_file
        self.temp.cleanup()

    def _transcribe(
        self,
        *,
        cover: TranscriptCover,
        snapshot: bool = True,
        workflow=None,
        cancelled=lambda: False,
    ) -> tuple[Transcript, _Engine]:
        media = self.root / "media.mp4"
        media.write_bytes(b"fake-media")
        engine = _Engine()

        def resolve(
            _source,
            _work_dir,
            _enabled,
            _progress,
            meta,
            **_kwargs,
        ):
            meta.update(cover.merge_meta(meta))
            meta["title"] = "可信作品"
            return media

        with (
            patch.object(pipeline, "resolve_source", side_effect=resolve),
            patch.object(
                pipeline, "to_wav_16k_mono", side_effect=lambda path, _out, **_kwargs: path
            ),
            patch.object(pipeline, "get_engine", return_value=engine),
        ):
            result = pipeline.transcribe_source(
                "https://www.youtube.com/watch?v=trusted",
                cover_snapshot=CollectCoverPreferenceSnapshot(snapshot),
                cover_workflow=workflow,
                is_cancelled=cancelled,
            )
        return result, engine


    def test_official_platform_http_thumbnail_is_upgraded_to_https(self):
        cases = (
            (
                {
                    "extractor_key": "BiliBili",
                    "id": "BV1abc",
                    "webpage_url": "https://www.bilibili.com/video/BV1abc",
                    "thumbnail": "http://i2.hdslb.com/bfs/archive/a.jpg",
                },
                "https://i2.hdslb.com/bfs/archive/a.jpg",
            ),
            (
                {
                    "extractor_key": "XiaoHongShu",
                    "id": "note-1",
                    "webpage_url": "https://www.xiaohongshu.com/explore/note-1",
                    "thumbnail": "http://sns-webpic-qc.xhscdn.com/a.webp",
                },
                "https://sns-webpic-qc.xhscdn.com/a.webp",
            ),
        )
        with patch.object(extractor, "_bilibili_stats_author", return_value=({}, {})), patch("urllib.request.urlopen", side_effect=AssertionError("extra metadata request")):
            for info, expected in cases:
                with self.subTest(expected=expected):
                    meta = {}
                    extractor._fill_meta(meta, info)
                    self.assertEqual(meta["cover_url"], expected)
                    self.assertEqual(meta["cover_status"], "url_only")


if __name__ == "__main__":
    unittest.main()
