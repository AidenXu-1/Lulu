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

    def test_douyin_identity_binds_id_and_cover_from_same_work_object(self):
        blobs = [
            {
                "loader": {
                    "aweme_detail": {
                        "aweme_id": "735001",
                        "video": {
                            "cover": {
                                "url_list": [
                                    "https://p3.example.test/cover.jpeg?token=redacted"
                                ]
                            }
                        },
                    }
                }
            }
        ]
        self.assertEqual(
            extractor._douyin_cover_identity(blobs),
            (
                "735001",
                "https://p3.example.test/cover.jpeg?token=redacted",
            ),
        )

        mismatched = [
            {"aweme_id": "735001"},
            {"video": {"cover": {"url_list": ["https://p3.test/c.jpg"]}}},
        ]
        self.assertEqual(
            extractor._douyin_cover_identity(mismatched), ("", "")
        )

    def test_douyin_source_gate_parses_real_hostname_and_rejects_spoofs(self):
        spoofed = (
            "https://v.douyin.com@attacker.invalid/share",
            "https://douyin.com.attacker.example/watch/forged",
            "https://attacker.invalid/path/douyin.com/video/1",
            "https://attacker.invalid/?next=https://v.douyin.com/abc",
            "https://attacker.invalid/#https://iesdouyin.com/share/video/1",
            "https://DOUYIN.COM.attacker.example/video/1",
            "https://douyin.com./video/1",
            "https://douyin.com:443/video/1",
            "https://douyin.com:not-a-port/video/1",
            "https://%64ouyin.com/video/1",
            "https://douyin%2ecom/video/1",
            "https:///douyin.com/video/1",
            "http://douyin.com/video/1",
            "https://reader@douyin.com/video/1",
            "https://douyin。com/video/1",
        )
        for source in spoofed:
            with self.subTest(source=source):
                self.assertFalse(extractor.is_douyin(source))

    def test_official_douyin_urls_and_share_text_keep_specialized_route(self):
        sources = (
            "https://www.douyin.com/video/735003",
            "https://v.douyin.com/AbCdEf/",
            "https://www.iesdouyin.com/share/video/735003",
            "https://V.DOUYIN.COM/AbCdEf/",
            "复制此链接打开抖音 https://v.douyin.com/AbCdEf/ 查看作品",
        )
        media = self.root / "official.mp4"
        media.write_bytes(b"fixture")
        for source in sources:
            with self.subTest(source=source):
                with (
                    patch.object(
                        extractor,
                        "_download_douyin",
                        return_value=media,
                    ) as douyin_download,
                    patch.object(
                        extractor,
                        "_download_via_ytdlp",
                        side_effect=AssertionError("官方抖音链接不得降级到通用下载"),
                    ),
                ):
                    resolved = extractor.resolve_source(
                        source,
                        self.root,
                        True,
                    )
                self.assertEqual(resolved, media)
                douyin_download.assert_called_once()

    def test_douyin_tls_and_cover_identity_fail_closed(self):
        self.assertTrue(extractor._SSL_CTX.check_hostname)
        self.assertEqual(extractor._SSL_CTX.verify_mode, ssl.CERT_REQUIRED)

        def fake_stream(_url, dst, _progress, **_kwargs):
            dst.write_bytes(b"\x00" * 64 + b"ftypmp42" + b"\x00" * 64)
            return dst

        cases = (
            (
                "https://douyin.com.attacker.example/video/1",
                "735004",
                "https://cover.example/735004.jpg",
            ),
            (
                "https://www.douyin.com/video/1",
                "forged-work-id",
                "https://cover.example/forged.jpg",
            ),
            (
                "https://www.douyin.com/video/1",
                "735005",
                "http://cover.example/735005.jpg",
            ),
        )
        for source, work_id, cover_url in cases:
            with self.subTest(source=source, work_id=work_id, cover_url=cover_url):
                meta = {}
                with (
                    patch.object(
                        extractor,
                        "_douyin_play_url",
                        return_value=(
                            "https://media.example/video.mp4",
                            "作品",
                            {},
                            {},
                            work_id,
                            cover_url,
                        ),
                    ),
                    patch.object(
                        extractor,
                        "_stream_download",
                        side_effect=fake_stream,
                    ),
                ):
                    extractor._download_douyin(
                        source,
                        self.root,
                        lambda *_: None,
                        meta,
                    )
                self.assertEqual(meta.get("cover_platform", ""), "")
                self.assertEqual(meta.get("cover_work_id", ""), "")
                self.assertNotEqual(
                    meta.get("cover_status", "not_requested"),
                    "downloaded",
                )

    def test_spoofed_douyin_sources_never_reach_cover_workflow(self):
        sources = (
            "https://v.douyin.com@attacker.invalid/share",
            "https://douyin.com.attacker.example/watch/forged",
        )

        def fake_generic(
            _url,
            work_dir,
            _progress,
            meta,
            **_kwargs,
        ):
            media = work_dir / "generic.mp4"
            media.write_bytes(b"fixture")
            meta.update(
                TranscriptCover(
                    url="https://cover.fixture.invalid/forged.jpg?opaque=1",
                    status="url_only",
                ).merge_meta(meta)
            )
            return media

        for source in sources:
            with self.subTest(source=source):
                workflow = _Workflow(_success_receipt("douyin"))
                engine = _Engine()
                with (
                    patch.object(
                        extractor,
                        "_download_douyin",
                        side_effect=AssertionError("伪造主机不得进入抖音专用链"),
                    ) as douyin_download,
                    patch.object(
                        extractor,
                        "_download_via_ytdlp",
                        side_effect=fake_generic,
                    ) as generic_download,
                    patch(
                        "src.core.audio.download_process.download_in_process",
                        side_effect=lambda url, root, progress, meta, **kwargs:
                            extractor._download_via_ytdlp(
                                url, root, progress, meta,
                                link_media_mode=kwargs["link_media_mode"],
                            ),
                    ),
                    patch.object(
                        pipeline,
                        "resolve_source",
                        side_effect=extractor.resolve_source,
                    ),
                    patch.object(
                        pipeline,
                        "to_wav_16k_mono",
                        side_effect=lambda path, _out, **_kwargs: path,
                    ),
                    patch.object(pipeline, "get_engine", return_value=engine),
                ):
                    transcript = pipeline.transcribe_source(
                        source,
                        cover_snapshot=CollectCoverPreferenceSnapshot(True),
                        cover_workflow=workflow,
                    )
                douyin_download.assert_not_called()
                generic_download.assert_called_once()
                self.assertEqual(workflow.calls, [])
                self.assertEqual(transcript.cover.platform, "")
                self.assertEqual(transcript.cover.work_id, "")
                self.assertEqual(transcript.cover.status, "url_only")

    def test_douyin_download_writes_bound_identity_without_network(self):
        meta = {}

        def fake_stream(_url, dst, _progress, **_kwargs):
            dst.write_bytes(b"\x00" * 64 + b"ftypmp42" + b"\x00" * 64)
            return dst

        with (
            patch.object(
                extractor,
                "_douyin_play_url",
                return_value=(
                    "https://media.example/video.mp4",
                    "作品",
                    {},
                    {},
                    "735002",
                    "https://cover.example/735002.jpg",
                ),
            ),
            patch.object(extractor, "_stream_download", side_effect=fake_stream),
        ):
            path = extractor._download_douyin(
                "https://v.douyin.com/example",
                self.root,
                lambda *_: None,
                meta,
            )

        self.assertTrue(path.exists())
        self.assertEqual(meta["cover_platform"], "douyin")
        self.assertEqual(meta["cover_work_id"], "735002")
        self.assertEqual(
            meta["cover_url"], "https://cover.example/735002.jpg"
        )
        self.assertEqual(meta["cover_status"], "url_only")

    def test_douyin_public_blob_preserves_archive_metadata_without_network(self):
        blobs = [
            {
                "aweme_detail": {
                    "aweme_id": "735002",
                    "create_time": 1785686400,
                    "video": {"duration": 95400},
                }
            }
        ]
        facts = extractor._douyin_work_facts(blobs)
        meta = {}

        extractor._apply_douyin_work_meta(
            meta,
            share_url="https://v.douyin.com/example",
            play_url="https://v26.douyinvod.com/video.mp4",
            facts=facts,
        )

        self.assertEqual(meta["platform"], "douyin")
        self.assertEqual(meta["work_id"], "735002")
        self.assertEqual(
            meta["webpage_url"],
            "https://www.douyin.com/video/735002",
        )
        self.assertEqual(meta["upload_date"], "20260803")
        self.assertEqual(meta["duration"], 95.4)
        self.assertEqual(
            meta["media_url"],
            "https://v26.douyinvod.com/video.mp4",
        )

    def test_ytdlp_identity_accepts_only_known_extractor_and_official_domain(self):
        cases = (
            (
                {
                    "extractor_key": "Youtube",
                    "id": "yt-1",
                    "webpage_url": "https://www.youtube.com/watch?v=yt-1",
                    "thumbnail": "https://i.ytimg.com/vi/yt-1/maxresdefault.jpg",
                },
                ("youtube", "yt-1"),
            ),
            (
                {
                    "extractor_key": "BiliBili",
                    "id": "BV1abc",
                    "webpage_url": "https://www.bilibili.com/video/BV1abc",
                    "thumbnail": "https://i0.hdslb.com/bfs/archive/a.jpg",
                },
                ("bilibili", "BV1abc"),
            ),
            (
                {
                    "extractor_key": "XiaoHongShu",
                    "id": "note-1",
                    "webpage_url": "https://www.xiaohongshu.com/explore/note-1",
                    "thumbnail": "https://sns-img.example.test/note.webp",
                },
                ("xiaohongshu", "note-1"),
            ),
        )
        with (
            patch.object(
                extractor,
                "_bilibili_stats_author",
                return_value=({}, {}),
            ) as bilibili_stats,
            patch("urllib.request.urlopen", side_effect=AssertionError("extra metadata request")) as fetch,
        ):
            for info, expected in cases:
                with self.subTest(expected=expected):
                    meta = {}
                    extractor._fill_meta(meta, info)
                    self.assertEqual(
                        (
                            meta["cover_platform"],
                            meta["cover_work_id"],
                        ),
                        expected,
                    )
                    self.assertEqual(meta["cover_status"], "url_only")
        bilibili_stats.assert_called_once()
        fetch.assert_not_called()

        spoofed = {
            "extractor_key": "Youtube",
            "id": "stolen",
            "webpage_url": "https://attacker.example/watch/stolen",
            "thumbnail": "https://cdn.example/cover.jpg",
        }
        meta = {}
        extractor._fill_meta(meta, spoofed)
        self.assertEqual(meta["cover_platform"], "")
        self.assertEqual(meta["cover_work_id"], "")
        self.assertEqual(meta["cover_status"], "url_only")
        self.assertEqual(meta["cover_url"], spoofed["thumbnail"])

    def test_non_https_thumbnail_is_not_promoted_to_cover_url(self):
        meta = {}
        extractor._fill_meta(
            meta,
            {
                "extractor_key": "Youtube",
                "id": "yt-1",
                "webpage_url": "https://youtube.com/watch?v=yt-1",
                "thumbnail": "http://cdn.example/cover.jpg",
            },
        )
        self.assertEqual(meta["cover_url"], "")
        self.assertEqual(meta["cover_status"], "not_requested")
        self.assertEqual(meta["cover_platform"], "youtube")
        self.assertEqual(meta["cover_work_id"], "yt-1")

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

    def test_successful_cover_is_separate_from_successful_transcript(self):
        workflow = _Workflow(_success_receipt())
        transcript, engine = self._transcribe(
            cover=TranscriptCover(
                "youtube",
                "yt-1",
                "https://i.ytimg.com/cover.jpg",
                "url_only",
            ),
            workflow=workflow,
        )
        self.assertEqual(transcript.text, "逐字稿成功")
        self.assertEqual(engine.calls, 1)
        self.assertEqual(transcript.cover.status, "downloaded")
        self.assertEqual(
            transcript.cover.local_path,
            "youtube/0123456789abcdef0123456789abcdef.jpg",
        )
        self.assertEqual(len(workflow.calls), 1)
        self.assertEqual(workflow.calls[0]["platform"], "youtube")
        self.assertNotIn("url", repr(workflow.calls[0]["is_cancelled"]))

    def test_supported_platforms_share_the_same_single_link_receipt_contract(self):
        for platform in ("douyin", "xiaohongshu", "bilibili", "youtube"):
            with self.subTest(platform=platform):
                workflow = _Workflow(_success_receipt(platform))
                transcript, engine = self._transcribe(
                    cover=TranscriptCover(
                        platform,
                        f"{platform}-1",
                        f"https://cdn.example/{platform}.jpg",
                        "url_only",
                    ),
                    workflow=workflow,
                )
                self.assertEqual(engine.calls, 1)
                self.assertEqual(transcript.cover.status, "downloaded")
                self.assertTrue(
                    transcript.cover.local_path.startswith(f"{platform}/")
                )

    def test_cover_failure_does_not_change_transcript_success_or_url(self):
        workflow = _Workflow(CoverReceipt.failed("cover_timeout"))
        transcript, engine = self._transcribe(
            cover=TranscriptCover(
                "youtube",
                "yt-2",
                "https://i.ytimg.com/cover.jpg?opaque=1",
                "url_only",
            ),
            workflow=workflow,
        )
        self.assertEqual(transcript.text, "逐字稿成功")
        self.assertEqual(engine.calls, 1)
        self.assertEqual(transcript.cover.status, "failed")
        self.assertEqual(transcript.cover.error_code, "cover_timeout")
        self.assertEqual(
            transcript.cover.url,
            "https://i.ytimg.com/cover.jpg?opaque=1",
        )
        self.assertEqual(transcript.cover.local_path, "")

    def test_cover_cancel_receipt_keeps_url_only_and_transcript_success(self):
        workflow = _Workflow(CoverReceipt.failed("cover_cancelled"))
        transcript, engine = self._transcribe(
            cover=TranscriptCover(
                "youtube",
                "yt-3",
                "https://i.ytimg.com/cover.jpg",
                "url_only",
            ),
            workflow=workflow,
        )
        self.assertEqual(engine.calls, 1)
        self.assertEqual(transcript.text, "逐字稿成功")
        self.assertEqual(transcript.cover.status, "url_only")
        self.assertEqual(transcript.cover.error_code, "")
        self.assertEqual(transcript.cover.local_path, "")

    def test_identity_shortfall_and_disabled_snapshot_never_call_workflow(self):
        for cover, enabled, expected_status in (
            (
                TranscriptCover(
                    "",
                    "",
                    "https://cdn.example/cover.jpg",
                    "url_only",
                ),
                True,
                "url_only",
            ),
            (
                TranscriptCover(
                    "youtube",
                    "yt-4",
                    "https://cdn.example/cover.jpg",
                    "url_only",
                ),
                False,
                "url_only",
            ),
            (
                TranscriptCover(
                    "douyin",
                    "735006",
                    "http://cdn.example/cover.jpg",
                    "not_requested",
                ),
                True,
                "not_requested",
            ),
        ):
            with self.subTest(cover=cover, enabled=enabled):
                workflow = _Workflow(_success_receipt())
                transcript, _engine = self._transcribe(
                    cover=cover,
                    snapshot=enabled,
                    workflow=workflow,
                )
                self.assertEqual(workflow.calls, [])
                self.assertEqual(transcript.cover.status, expected_status)
                self.assertEqual(transcript.cover.local_path, "")

    def test_production_default_has_no_cover_workflow(self):
        transcript, _engine = self._transcribe(
            cover=TranscriptCover(
                "youtube",
                "yt-5",
                "https://cdn.example/cover.jpg",
                "url_only",
            ),
            snapshot=True,
            workflow=None,
        )
        self.assertEqual(transcript.cover.status, "url_only")
        self.assertEqual(transcript.cover.local_path, "")

    def test_invalid_success_receipt_fails_closed_without_absolute_path(self):
        bad = CoverReceipt(
            status="downloaded",
            relative_path="/Users/example/cover.jpg",
            size=128,
            sha256="a" * 64,
            mime_type="image/jpeg",
        )
        transcript, engine = self._transcribe(
            cover=TranscriptCover(
                "youtube",
                "yt-6",
                "https://cdn.example/cover.jpg",
                "url_only",
            ),
            workflow=_Workflow(bad),
        )
        self.assertEqual(engine.calls, 1)
        self.assertEqual(transcript.cover.status, "failed")
        self.assertEqual(transcript.cover.error_code, "cover_commit_failed")
        self.assertEqual(transcript.cover.local_path, "")
        self.assertNotIn("/Users/", json.dumps(transcript.meta))

    def test_worker_freezes_preference_snapshot_at_construction(self):
        preference = _Preference(True)
        workflow = _Workflow(_success_receipt())
        seen = {}

        def fake_transcribe(*_args, **kwargs):
            seen["snapshot"] = kwargs["cover_snapshot"]
            seen["workflow"] = kwargs["cover_workflow"]
            return Transcript("ok")

        with patch.object(
            worker_module, "transcribe_source", side_effect=fake_transcribe
        ):
            worker = worker_module.TranscribeWorker(
                "https://youtube.com/watch?v=1",
                "local_sensevoice",
                True,
                cover_preference_service=preference,
                cover_workflow=workflow,
            )
            preference.enabled = False
            worker.run()

        self.assertEqual(preference.calls, 1)
        self.assertTrue(seen["snapshot"].include_cover)
        self.assertIs(seen["workflow"], workflow)

    def test_worker_without_injection_freezes_disabled_snapshot(self):
        seen = {}

        def fake_transcribe(*_args, **kwargs):
            seen.update(kwargs)
            return Transcript("ok")

        with patch.object(
            worker_module, "transcribe_source", side_effect=fake_transcribe
        ):
            worker_module.TranscribeWorker(
                "local.wav", "local_sensevoice", False
            ).run()
        self.assertFalse(seen["cover_snapshot"].include_cover)
        self.assertIsNone(seen["cover_workflow"])

    def test_library_meta_round_trip_and_listing_preserve_cover_receipt(self):
        transcript = Transcript(
            "正文",
            title="标题",
            meta=TranscriptCover(
                "youtube",
                "yt-7",
                "https://cdn.example/cover.jpg",
                "downloaded",
                "youtube/0123456789abcdef0123456789abcdef.jpg",
            ).merge_meta({"thumbnail": "https://legacy.example/thumb.jpg"}),
        )
        entry_id = library.save(transcript, "标题")
        loaded = library.load(entry_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.text, "正文")
        self.assertEqual(loaded.cover, transcript.cover)

        listed = next(e for e in library.list_entries() if e["id"] == entry_id)
        self.assertEqual(listed["cover_status"], "downloaded")
        self.assertEqual(
            listed["cover_local_path"],
            "youtube/0123456789abcdef0123456789abcdef.jpg",
        )
        on_disk = json.loads(
            (library.LIBRARY_DIR / f"{entry_id}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(
            Path(on_disk["meta"]["cover_local_path"]).is_absolute()
        )

    def test_legacy_thumbnail_remains_url_only_not_downloaded_evidence(self):
        transcript = Transcript(
            "正文",
            meta={"thumbnail": "https://legacy.example/thumb.jpg"},
        )
        entry_id = library.save(transcript, "旧文稿")
        loaded = library.load(entry_id)
        assert loaded is not None
        self.assertEqual(loaded.cover.url, transcript.meta["thumbnail"])
        self.assertEqual(loaded.cover.status, "url_only")
        self.assertEqual(loaded.cover.local_path, "")

    def test_local_transcript_meta_is_not_polluted_with_empty_cover_fields(self):
        entry_id = library.save(
            Transcript("本地正文", meta={"origin": "local"}), "本地文稿"
        )
        on_disk = json.loads(
            (library.LIBRARY_DIR / f"{entry_id}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(on_disk["meta"], {"origin": "local"})

    def test_local_file_pipeline_never_calls_cover_workflow(self):
        source = self.root / "local.wav"
        source.write_bytes(b"local")
        engine = _Engine()
        workflow = _Workflow(_success_receipt())

        with (
            patch.object(pipeline, "resolve_source", return_value=source),
            patch.object(
                pipeline,
                "to_wav_16k_mono",
                side_effect=lambda path, _out, **_kwargs: path,
            ),
            patch.object(pipeline, "get_engine", return_value=engine),
        ):
            transcript = pipeline.transcribe_source(
                str(source),
                cover_snapshot=CollectCoverPreferenceSnapshot(True),
                cover_workflow=workflow,
            )
        self.assertEqual(transcript.text, "逐字稿成功")
        self.assertEqual(workflow.calls, [])
        self.assertEqual(transcript.meta, {})

    def test_cancelled_pipeline_removes_its_transient_work_directory(self):
        seen: dict[str, Path] = {}
        def cancelled():
            return "work_dir" in seen

        def resolve(
            _source,
            work_dir,
            _enabled,
            _progress,
            meta,
            **_kwargs,
        ):
            seen["work_dir"] = work_dir
            part = work_dir / "download.part"
            part.write_bytes(b"partial")
            meta.update(
                TranscriptCover(
                    "youtube",
                    "yt-cancel",
                    "https://cdn.example/cover.jpg",
                    "url_only",
                ).merge_meta(meta)
            )
            return part

        with (
            patch.object(pipeline, "resolve_source", side_effect=resolve),
            self.assertRaises(Exception) as raised,
        ):
            pipeline.transcribe_source(
                "https://youtube.com/watch?v=cancel",
                is_cancelled=cancelled,
            )
        self.assertTrue(pipeline.was_cancelled(raised.exception))
        self.assertFalse(seen["work_dir"].exists())

    def test_zz_external_guards_record_zero_calls(self):
        self.assertEqual(self.external_calls, [])


if __name__ == "__main__":
    unittest.main()
