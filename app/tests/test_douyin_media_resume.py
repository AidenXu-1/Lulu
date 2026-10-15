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


if __name__ == "__main__":
    unittest.main()
