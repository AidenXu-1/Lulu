import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

from src.core.synthesis import install_support as support


class Response:
    def __init__(self, *, status, headers, chunks, url):
        self.status = status
        self.headers = headers
        self.chunks = list(chunks)
        self.url = url
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True
        return False

    def geturl(self):
        return self.url

    def read(self, _size):
        if not self.chunks:
            return b""
        value = self.chunks.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class TTSInstallSupportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_log = support.diagnostics_path
        support.diagnostics_path = lambda: self.root / "logs" / "install.jsonl"  # type: ignore[assignment]

    def tearDown(self):
        support.diagnostics_path = self.old_log  # type: ignore[assignment]
        self.temp.cleanup()

    def identity(self, payload: bytes) -> support.DownloadIdentity:
        return support.DownloadIdentity(
            url="https://github.com/AidenXu-1/lulu-runtimes/releases/download/test/runtime.tar.gz",
            filename="runtime.tar.gz",
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            source="github_release",
        )

    def test_modelscope_source_accepts_official_cdn_subdomain_only(self):
        accepted = (
            "https://modelscope.cn/file.zip",
            "https://www.modelscope.cn/file.zip",
            "https://cdn-lfs-cn-1.modelscope.cn/file.zip?auth_key=public",
        )
        for url in accepted:
            with self.subTest(url=url):
                support.validate_source_url(url, "modelscope_g2pw")

        rejected = (
            "https://modelscope.cn.evil.example/file.zip",
            "https://evil-modelscope.cn/file.zip",
            "http://cdn-lfs-cn-1.modelscope.cn/file.zip",
            "https://user@cdn-lfs-cn-1.modelscope.cn/file.zip",
            "https://cdn-lfs-cn-1.modelscope.cn:444/file.zip",
            "https://cdn-lfs-cn-1.modelscope.cn/file.zip#fragment",
        )
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(support.InstallError) as caught:
                support.validate_source_url(url, "modelscope_g2pw")
            self.assertEqual(caught.exception.code, "unsafe_url")

    def test_redirect_handler_validates_next_hop_before_request_creation(self):
        handler = support._SourceRedirectHandler("modelscope_g2pw")
        request = urllib.request.Request("https://www.modelscope.cn/file.zip")

        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "https://cdn-lfs-cn-1.modelscope.cn/file.zip"
        )
        self.assertEqual(redirected.full_url, "https://cdn-lfs-cn-1.modelscope.cn/file.zip")

        with self.assertRaises(support.InstallError) as caught:
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://modelscope.cn.evil.example/file.zip"
            )
        self.assertEqual(caught.exception.code, "unsafe_url")

    def seed_resume(self, identity, payload: bytes) -> tuple[Path, Path]:
        directory = self.root / "downloads"
        directory.mkdir(parents=True)
        part = directory / f"{identity.filename}.part"
        meta = directory / f"{identity.filename}.part.json"
        part.write_bytes(payload)
        os.chmod(part, 0o600)
        support._atomic_json(meta, support._meta_payload(identity, len(payload)))
        return part, meta

    def test_exact_range_resume_appends_and_verifies(self):
        payload = b"0123456789abcdef"
        identity = self.identity(payload)
        self.seed_resume(identity, payload[:4])
        requests = []

        def open_fixture(request, **_kwargs):
            requests.append(request)
            return Response(
                status=206,
                headers={
                    "Content-Length": str(len(payload) - 4),
                    "Content-Range": f"bytes 4-{len(payload)-1}/{len(payload)}",
                },
                chunks=[payload[4:]],
                url=identity.url,
            )

        final = support.resumable_download(
            identity, self.root / "downloads", lambda *_: None, lambda: False,
            label="fixture", model_id="local_voxcpm2", urlopen=open_fixture, sleep=lambda _s: None,
        )

        self.assertEqual(final.read_bytes(), payload)
        self.assertEqual(requests[0].headers["Range"], "bytes=4-")
        self.assertFalse((self.root / "downloads/runtime.tar.gz.part.json").exists())

    def test_server_ignoring_range_restarts_without_duplicate_bytes(self):
        payload = b"abcdefghij"
        identity = self.identity(payload)
        self.seed_resume(identity, payload[:3])
        requests = []
        responses = [
            Response(status=200, headers={"Content-Length": str(len(payload))}, chunks=[payload], url=identity.url),
            Response(status=200, headers={"Content-Length": str(len(payload))}, chunks=[payload], url=identity.url),
        ]

        def open_fixture(request, **_kwargs):
            requests.append(request)
            return responses.pop(0)

        final = support.resumable_download(
            identity, self.root / "downloads", lambda *_: None, lambda: False,
            label="fixture", model_id="local_voxcpm2", urlopen=open_fixture, sleep=lambda _s: None,
        )

        self.assertEqual(final.read_bytes(), payload)
        self.assertEqual(requests[0].headers["Range"], "bytes=3-")
        self.assertNotIn("Range", requests[1].headers)

    def test_repeated_network_failure_preserves_last_checkpoint(self):
        payload = b"abcdefgh"
        identity = self.identity(payload)
        requests = []

        def open_fixture(request, **_kwargs):
            requests.append(request)
            offset = 0 if "Range" not in request.headers else int(request.headers["Range"].split("=")[1][:-1])
            chunks = [payload[:4], OSError("offline")] if offset == 0 else [OSError("offline")]
            headers = {"Content-Length": str(len(payload) - offset)}
            status = 200
            if offset:
                status = 206
                headers["Content-Range"] = f"bytes {offset}-{len(payload)-1}/{len(payload)}"
            return Response(status=status, headers=headers, chunks=chunks, url=identity.url)

        with self.assertRaisesRegex(support.InstallError, "已保留当前进度") as caught:
            support.resumable_download(
                identity, self.root / "downloads", lambda *_: None, lambda: False,
                label="fixture", model_id="local_voxcpm2", urlopen=open_fixture, sleep=lambda _s: None,
            )

        self.assertEqual(caught.exception.code, "network")
        part = self.root / "downloads/runtime.tar.gz.part"
        meta = self.root / "downloads/runtime.tar.gz.part.json"
        self.assertEqual(part.read_bytes(), payload[:4])
        self.assertEqual(json.loads(meta.read_text())["downloaded_bytes"], 4)
        self.assertEqual(len(requests), 3)

    def test_sha_mismatch_discards_untrusted_download(self):
        expected = b"trusted"
        actual = b"corrupt"
        identity = support.DownloadIdentity(
            url="https://github.com/AidenXu-1/lulu-runtimes/releases/download/test/runtime.tar.gz",
            filename="runtime.tar.gz",
            sha256=hashlib.sha256(expected).hexdigest(),
            size=len(actual),
            source="github_release",
        )
        response = Response(
            status=200, headers={"Content-Length": str(len(actual))}, chunks=[actual], url=identity.url,
        )

        with self.assertRaisesRegex(support.InstallError, "校验失败"):
            support.resumable_download(
                identity, self.root / "downloads", lambda *_: None, lambda: False,
                label="fixture", model_id="local_voxcpm2",
                urlopen=lambda *_a, **_k: response, sleep=lambda _s: None,
            )

        self.assertFalse((self.root / "downloads/runtime.tar.gz").exists())
        self.assertFalse((self.root / "downloads/runtime.tar.gz.part").exists())
        self.assertFalse((self.root / "downloads/runtime.tar.gz.part.json").exists())

    def test_resume_metadata_drift_resets_to_zero(self):
        payload = b"abcdefgh"
        identity = self.identity(payload)
        part, meta = self.seed_resume(identity, payload[:4])
        value = json.loads(meta.read_text())
        value["sha256"] = "0" * 64
        support._atomic_json(meta, value)
        seen = []

        def open_fixture(request, **_kwargs):
            seen.append(request)
            return Response(
                status=200, headers={"Content-Length": str(len(payload))}, chunks=[payload], url=identity.url,
            )

        final = support.resumable_download(
            identity, part.parent, lambda *_: None, lambda: False,
            label="fixture", model_id="local_voxcpm2", urlopen=open_fixture, sleep=lambda _s: None,
        )
        self.assertEqual(final.read_bytes(), payload)
        self.assertNotIn("Range", seen[0].headers)

    def test_wrong_content_range_discards_stale_prefix_then_restarts(self):
        payload = b"abcdefgh"
        identity = self.identity(payload)
        self.seed_resume(identity, payload[:3])
        requests = []
        responses = [
            Response(
                status=206,
                headers={"Content-Length": "5", "Content-Range": "bytes 2-6/8"},
                chunks=[payload[3:]],
                url=identity.url,
            ),
            Response(
                status=200,
                headers={"Content-Length": str(len(payload))},
                chunks=[payload],
                url=identity.url,
            ),
        ]

        def open_fixture(request, **_kwargs):
            requests.append(request)
            return responses.pop(0)

        final = support.resumable_download(
            identity, self.root / "downloads", lambda *_: None, lambda: False,
            label="fixture", model_id="local_voxcpm2", urlopen=open_fixture, sleep=lambda _s: None,
        )

        self.assertEqual(final.read_bytes(), payload)
        self.assertEqual(requests[0].headers["Range"], "bytes=3-")
        self.assertNotIn("Range", requests[1].headers)

    def test_retry_backoff_can_be_cancelled_without_another_network_attempt(self):
        attempts = []
        cancelled = False

        def call(attempt):
            attempts.append(attempt)
            raise OSError("offline")

        def cancel_during_sleep(_seconds):
            nonlocal cancelled
            cancelled = True

        with self.assertRaises(support.CancelledError):
            support.retry_call(
                call,
                is_cancelled=lambda: cancelled,
                sleep=cancel_during_sleep,
            )

        self.assertEqual(attempts, [1])

    def test_disk_preflight_reports_required_and_available(self):
        usage = os.statvfs(self.root)
        _ = usage  # prove target exists on a real volume; disk_usage itself is mocked below
        with patch.object(support.shutil, "disk_usage", return_value=shutil_usage(100, 90, 10)):
            with self.assertRaisesRegex(support.InstallError, "磁盘空间不足") as caught:
                support.require_free_space(self.root, 20)
        self.assertEqual(caught.exception.code, "disk_space")

    def test_diagnostics_are_field_bounded_redacted_and_rotated(self):
        support.log_install_event(
            model_id="../../Users/aiden", event="failed?token=secret",
            phase="https://evil.test/path", source="github_release", error_code="network",
        )
        path = support.diagnostics_path()
        first = json.loads(path.read_text().splitlines()[0])
        self.assertEqual(tuple(first), support._LOG_FIELDS)
        raw = path.read_text()
        self.assertNotIn("/Users/", raw)
        self.assertNotIn("https://", raw)
        self.assertNotIn("secret", raw)
        self.assertEqual(first["model_id"], "invalid")

        path.write_bytes(b"x" * support.LOG_MAX_BYTES)
        os.chmod(path, 0o600)
        support.log_install_event(model_id="local_voxcpm2", event="retry")
        self.assertTrue(path.with_name("install.1.jsonl").is_file())
        self.assertEqual(json.loads(path.read_text())["event"], "retry")

    def test_diagnostics_refuse_symlink_without_touching_target(self):
        path = support.diagnostics_path()
        path.parent.mkdir(parents=True)
        outside = self.root / "outside.txt"
        outside.write_text("keep")
        path.symlink_to(outside)
        support.log_install_event(model_id="local_voxcpm2", event="failed")
        self.assertEqual(outside.read_text(), "keep")


def shutil_usage(total: int, used: int, free: int):
    usage = type("Usage", (), {})()
    usage.total = total
    usage.used = used
    usage.free = free
    return usage


if __name__ == "__main__":
    unittest.main()
