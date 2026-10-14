import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.audio import extractor
from src.core.models import Transcript
from src.core import pipeline
from src.ui import worker as worker_mod


class _FakeYDL:
    seen_opts: list[dict] = []

    def __init__(self, opts):
        self.opts = opts
        _FakeYDL.seen_opts.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def extract_info(self, _url, download=True):
        outtmpl = self.opts["outtmpl"]
        ext = "mp4" if self.opts["format"].startswith("bestvideo") else "m4a"
        path = Path(outtmpl.replace("%(id)s", "vid").replace("%(ext)s", ext))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media")
        for hook in self.opts.get("progress_hooks", []):
            hook({
                "status": "downloading",
                "downloaded_bytes": 25 * 1024 * 1024,
                "total_bytes": 100 * 1024 * 1024,
            })
            hook({"status": "finished", "filename": str(path)})
        return {"id": "vid", "title": "标题", "filepath": str(path)}

    def prepare_filename(self, info):
        ext = "mp4" if self.opts["format"].startswith("bestvideo") else "m4a"
        return str(Path(self.opts["outtmpl"].replace("%(id)s", info["id"]).replace("%(ext)s", ext)))


class LinkMediaModeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = patch("src.core.audio.download_runtime.deno_path", return_value=Path("/fixture/deno"))
        self.runtime.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_ytdlp = sys.modules.get("yt_dlp")
        _FakeYDL.seen_opts = []
        fake_mod = types.SimpleNamespace(
            YoutubeDL=_FakeYDL,
            version=types.SimpleNamespace(__version__="test"),
        )
        sys.modules["yt_dlp"] = fake_mod

    def tearDown(self):
        if self.old_ytdlp is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = self.old_ytdlp
        self.tmp.cleanup()
        self.runtime.stop()

    def test_ytdlp_audio_mode_keeps_fast_audio_format(self):
        events: list[tuple[str, float]] = []
        path = extractor._download_via_ytdlp(
            "https://www.youtube.com/watch?v=1",
            self.root,
            lambda message, fraction: events.append((message, fraction)),
            link_media_mode="audio",
        )

        self.assertEqual(path.suffix, ".m4a")
        self.assertEqual(_FakeYDL.seen_opts[-1]["format"], "bestaudio/best")
        self.assertNotIn("http_chunk_size", _FakeYDL.seen_opts[-1])
        self.assertNotIn("merge_output_format", _FakeYDL.seen_opts[-1])
        self.assertIn(0.25, [fraction for _message, fraction in events])
        self.assertTrue(any("25%" in message and "25.0 MB / 100.0 MB" in message for message, _fraction in events))
        self.assertEqual(events[-1][1], 1.0)

    def test_youtube_access_denial_does_not_repeat_seven_audio_plans(self):
        events = []
        with patch.object(_FakeYDL, "extract_info", side_effect=RuntimeError("HTTP Error 403: signed-secret")) as request:
            with self.assertRaises(RuntimeError) as caught:
                extractor._download_via_ytdlp(
                    "https://youtu.be/fixture", self.root,
                    lambda message, fraction: events.append(message),
                )
        self.assertEqual(request.call_count, 1)
        self.assertEqual(caught.exception.code, "CORE_DOWNLOAD_ACCESS_DENIED")
        self.assertNotIn("signed-secret", str(caught.exception))
        self.assertFalse(any("备用音轨" in message for message in events))

    def test_non_youtube_audio_keeps_existing_generic_download_contract(self):
        extractor._download_via_ytdlp(
            "https://www.bilibili.com/video/BV1xx",
            self.root,
            lambda *_: None,
            link_media_mode="audio",
        )

        self.assertEqual(_FakeYDL.seen_opts[-1]["format"], "bestaudio/best")
        self.assertNotIn("http_chunk_size", _FakeYDL.seen_opts[-1])

    def test_missing_formats_does_not_claim_the_work_is_an_image_post(self):
        with patch.object(_FakeYDL, "extract_info", side_effect=RuntimeError("No video formats found")):
            with self.assertRaisesRegex(RuntimeError, "未取得可下载") as caught:
                extractor._download_via_ytdlp(
                    "https://example.invalid/video", self.root, lambda *_: None,
                )
        self.assertNotIn("图文", str(caught.exception))

    def test_bare_youtube_domain_and_shorts_use_public_runtime_contract(self):
        extractor._download_via_ytdlp(
            "https://youtube.com/shorts/9AnydQCk7PU",
            self.root,
            lambda *_: None,
            link_media_mode="audio",
        )

        self.assertEqual(_FakeYDL.seen_opts[-1]["format"], "bestaudio/best")
        self.assertIn("%(format_id)s", _FakeYDL.seen_opts[-1]["outtmpl"])
        self.assertIn("deno", _FakeYDL.seen_opts[-1]["js_runtimes"])

    def test_pipeline_maps_stage_progress_and_never_reaches_100_before_commit(self):
        events: list[tuple[str, float]] = []
        old_resolve = pipeline.resolve_source
        old_to_wav = pipeline.to_wav_16k_mono
        old_get_engine = pipeline.get_engine

        class Engine:
            display_name = "Fake"

            def transcribe(self, wav_path, progress):
                progress("模型推理中", 0.5)
                progress("模型输出就绪", 1.0)
                return Transcript("ok")

        def fake_resolve(*args, **kwargs):
            args[3]("下载一半", 0.5)
            media = self.root / "media.m4a"
            media.write_bytes(b"media")
            return media

        try:
            pipeline.resolve_source = fake_resolve  # type: ignore[assignment]
            pipeline.to_wav_16k_mono = lambda media, out, **kwargs: media  # type: ignore[assignment]
            pipeline.get_engine = lambda _name=None: Engine()  # type: ignore[assignment]
            result = pipeline.transcribe_source(
                "https://example.com/v",
                progress=lambda message, fraction: events.append((message, fraction)),
            )
        finally:
            pipeline.resolve_source = old_resolve  # type: ignore[assignment]
            pipeline.to_wav_16k_mono = old_to_wav  # type: ignore[assignment]
            pipeline.get_engine = old_get_engine  # type: ignore[assignment]

        known = [fraction for _message, fraction in events if fraction >= 0]
        self.assertEqual(result.text, "ok")
        self.assertEqual(known, sorted(known))
        self.assertAlmostEqual(next(fraction for message, fraction in events if message == "下载一半"), 0.11)
        self.assertAlmostEqual(next(fraction for message, fraction in events if message == "模型推理中"), 0.635)
        self.assertLess(events[-2][1], 1.0)
        self.assertEqual(events[-1], ("完成", 1.0))

    def test_ytdlp_video_mode_downloads_full_video_format(self):
        path = extractor._download_via_ytdlp(
            "https://www.bilibili.com/video/BV1xx",
            self.root,
            lambda *_: None,
            link_media_mode="video",
        )

        self.assertEqual(path.suffix, ".mp4")
        self.assertEqual(_FakeYDL.seen_opts[-1]["format"], "bestvideo*+bestaudio/best")
        self.assertEqual(_FakeYDL.seen_opts[-1]["merge_output_format"], "mp4")

    def test_direct_download_resume_uses_full_range_total_and_monotonic_bytes(self):
        class Response:
            def __init__(self, body: bytes, headers: dict[str, str]):
                self.body = io.BytesIO(body)
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, size: int):
                return self.body.read(size)

            def geturl(self):
                return "https://cdn.example/media"

        calls = []
        responses = [
            Response(b"a" * 40, {"Content-Length": "100"}),
            Response(b"b" * 60, {"Content-Range": "bytes 40-99/100", "Content-Length": "60"}),
        ]

        def fake_urlopen(request, **_kwargs):
            calls.append(request.get_header("Range"))
            return responses.pop(0)

        events: list[tuple[str, float]] = []
        destination = self.root / "resume.bin"
        with patch.object(extractor.urllib.request, "urlopen", side_effect=fake_urlopen):
            extractor._stream_download(
                "https://cdn.example/media",
                destination,
                lambda message, fraction: events.append((message, fraction)),
                max_retries=2,
            )

        self.assertEqual(calls, [None, "bytes=40-"])
        self.assertEqual(destination.read_bytes(), b"a" * 40 + b"b" * 60)
        known = [fraction for _message, fraction in events if fraction >= 0]
        self.assertEqual(known, sorted(known))
        self.assertIn(0.4, known)
        self.assertEqual(known[-1], 1.0)

    def test_direct_download_restarts_safely_when_server_ignores_range(self):
        class Response:
            def __init__(self, body: bytes, headers: dict[str, str]):
                self.body = io.BytesIO(body)
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, size: int):
                return self.body.read(size)

            def geturl(self):
                return "https://cdn.example/media"

        responses = [
            Response(b"partial", {"Content-Length": "12"}),
            Response(b"complete-file", {"Content-Length": "13"}),
        ]
        destination = self.root / "restart.bin"
        with patch.object(
            extractor.urllib.request,
            "urlopen",
            side_effect=lambda *_args, **_kwargs: responses.pop(0),
        ):
            extractor._stream_download(
                "https://cdn.example/media",
                destination,
                lambda *_args: None,
                max_retries=2,
            )

        self.assertEqual(destination.read_bytes(), b"complete-file")

    def test_pipeline_forwards_link_media_mode_to_resolver(self):
        seen = {}
        old_resolve = pipeline.resolve_source
        old_to_wav = pipeline.to_wav_16k_mono
        old_get_engine = pipeline.get_engine

        class Engine:
            display_name = "Fake"

            def transcribe(self, wav_path, progress):
                return Transcript("ok")

        def fake_resolve(*args, **kwargs):
            seen["mode"] = kwargs.get("link_media_mode")
            media = self.root / "media.mp4"
            media.write_bytes(b"media")
            return media

        try:
            pipeline.resolve_source = fake_resolve  # type: ignore[assignment]
            pipeline.to_wav_16k_mono = lambda media, out, **kwargs: media  # type: ignore[assignment]
            pipeline.get_engine = lambda _name=None: Engine()  # type: ignore[assignment]
            pipeline.transcribe_source("https://example.com/v", link_media_mode="video")
        finally:
            pipeline.resolve_source = old_resolve  # type: ignore[assignment]
            pipeline.to_wav_16k_mono = old_to_wav  # type: ignore[assignment]
            pipeline.get_engine = old_get_engine  # type: ignore[assignment]

        self.assertEqual(seen["mode"], "video")

    def test_worker_forwards_link_media_mode(self):
        seen = {}
        old_transcribe = worker_mod.transcribe_source

        def fake_transcribe(*_args, **kwargs):
            seen["mode"] = kwargs.get("link_media_mode")
            return Transcript("ok")

        try:
            worker_mod.transcribe_source = fake_transcribe  # type: ignore[assignment]
            w = worker_mod.TranscribeWorker(
                "https://example.com/v",
                "local_sensevoice",
                True,
                link_media_mode="video",
            )
            w.run()
        finally:
            worker_mod.transcribe_source = old_transcribe  # type: ignore[assignment]

        self.assertEqual(seen["mode"], "video")

    def test_douyin_downloaded_media_validation_rejects_non_media(self):
        bad = self.root / "bad.mp4"
        bad.write_text("<html>captcha</html>", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "不是媒体文件"):
            extractor._ensure_downloaded_media_looks_valid(bad)

        broken = self.root / "broken.mp4"
        broken.write_bytes(b"not-an-mp4")
        with self.assertRaisesRegex(RuntimeError, "媒体文件头异常"):
            extractor._ensure_downloaded_media_looks_valid(broken)

        good = self.root / "good.mp4"
        good.write_bytes(b"\x00" * 80 + b"ftypmp42" + b"\x00" * 64)
        extractor._ensure_downloaded_media_looks_valid(good)

        ogg = self.root / "good.ogg"
        ogg.write_bytes(b"OggS" + b"\x00" * 64)
        extractor._ensure_downloaded_media_looks_valid(ogg)
