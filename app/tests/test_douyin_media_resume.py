from __future__ import annotations

from http.client import IncompleteRead
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.audio import extractor


class _Response:
    def __init__(self, *, status: int, headers: dict[str, str], parts):
        self.status = status
        self.headers = headers
        self._parts = iter(parts)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        value = next(self._parts, b"")
        if isinstance(value, BaseException):
            raise value
        return value

    def geturl(self):
        return "https://example.invalid/media"


class DouyinMediaResumeTests(unittest.TestCase):
    def test_bounded_retries_never_report_a_known_short_file_as_complete(self):
        responses = [
            _Response(
                status=200,
                headers={"Content-Length": "10"},
                parts=[b"0123", b""],
            ),
            _Response(
                status=206,
                headers={
                    "Content-Length": "2",
                    "Content-Range": "bytes 4-5/10",
                },
                parts=[b"45", b""],
            ),
        ]
        events = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            extractor.urllib.request, "urlopen", side_effect=responses
        ):
            target = Path(tmp) / "media.mp4"
            with self.assertRaisesRegex(RuntimeError, "文件不完整"):
                extractor._stream_download(
                    "https://example.invalid/media",
                    target,
                    lambda message, fraction: events.append((message, fraction)),
                    max_retries=2,
                )
            self.assertEqual(target.read_bytes(), b"012345")

        self.assertNotIn(("下载完成", 1.0), events)

    def test_stream_rejects_noncanonical_referer_before_network(self):
        unsafe = (
            "https://evil.example/video/7663085744302476579",
            "https://www.douyin.com/video/not-digits",
            "https://www.douyin.com/video/7663085744302476579?leak=1",
            "https://www.douyin.com/video/7663085744302476579\r\nX-Leak: true",
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "media.mp4"
            for referer in unsafe:
                with self.subTest(referer=referer), patch.object(
                    extractor.urllib.request, "urlopen"
                ) as open_mock:
                    with self.assertRaisesRegex(RuntimeError, "来源页"):
                        extractor._stream_download(
                            "https://example.invalid/media",
                            target,
                            lambda *_: None,
                            referer=referer,
                        )
                    open_mock.assert_not_called()

    def test_origin_referer_survives_interrupted_range_resume(self):
        requests = []
        responses = [
            _Response(
                status=200,
                headers={"Content-Length": "10"},
                parts=[b"0123", IncompleteRead(b"", 6)],
            ),
            _Response(
                status=206,
                headers={
                    "Content-Length": "6",
                    "Content-Range": "bytes 4-9/10",
                },
                parts=[b"456789", b""],
            ),
        ]

        def open_fake(request, **_kwargs):
            requests.append(request)
            return responses.pop(0)

        referer = "https://www.douyin.com/video/7663085744302476579"
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            extractor.urllib.request, "urlopen", side_effect=open_fake
        ):
            target = Path(tmp) / "media.mp4"
            extractor._stream_download(
                "https://example.invalid/media",
                target,
                lambda *_: None,
                referer=referer,
            )
            self.assertEqual(target.read_bytes(), b"0123456789")

        self.assertEqual(
            [request.get_header("Referer") for request in requests],
            [referer, referer],
        )
        self.assertIsNone(requests[0].get_header("Range"))
        self.assertEqual(requests[1].get_header("Range"), "bytes=4-")

    def test_server_ignoring_range_restarts_instead_of_appending_full_body(self):
        requests = []
        responses = [
            _Response(
                status=200,
                headers={"Content-Length": "10"},
                parts=[b"0123", IncompleteRead(b"", 6)],
            ),
            _Response(
                status=200,
                headers={"Content-Length": "10"},
                parts=[b"0123456789", b""],
            ),
        ]

        def open_fake(request, **_kwargs):
            requests.append(request)
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            extractor.urllib.request, "urlopen", side_effect=open_fake
        ):
            target = Path(tmp) / "media.mp4"
            extractor._stream_download("https://example.invalid/media", target, lambda *_: None)
            self.assertEqual(target.read_bytes(), b"0123456789")

        self.assertEqual(requests[1].get_header("Range"), "bytes=4-")

    def test_partial_response_must_start_at_requested_byte(self):
        responses = [
            _Response(
                status=200,
                headers={"Content-Length": "10"},
                parts=[b"0123", IncompleteRead(b"", 6)],
            ),
            _Response(
                status=206,
                headers={"Content-Length": "6", "Content-Range": "bytes 4-9/10"},
                parts=[b"456789", b""],
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            extractor.urllib.request, "urlopen", side_effect=responses
        ):
            target = Path(tmp) / "media.mp4"
            extractor._stream_download("https://example.invalid/media", target, lambda *_: None)
            self.assertEqual(target.read_bytes(), b"0123456789")


if __name__ == "__main__":
    unittest.main()
