import io
import http.client
import multiprocessing
import os
import socket
import shutil
import sys
import tarfile
import tempfile
import types
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from src.core.synthesis import runtime_packages as rp


def _spawn_guarded_fake_snapshot(repo: str, revision: str, cache_dir: str) -> dict:
    """Spawn-safe fixture: any real DNS/socket/HTTP call fails in the child."""
    calls = {"dns": 0, "socket": 0, "http": 0}

    def forbid_dns(*_args, **_kwargs):
        calls["dns"] += 1
        raise AssertionError("spawn fixture touched DNS")

    def forbid_socket(*_args, **_kwargs):
        calls["socket"] += 1
        raise AssertionError("spawn fixture touched socket")

    def forbid_http(*_args, **_kwargs):
        calls["http"] += 1
        raise AssertionError("spawn fixture touched HTTP")

    socket.getaddrinfo = forbid_dns
    socket.create_connection = forbid_socket
    socket.socket = forbid_socket
    urllib.request.urlopen = forbid_http
    http.client.HTTPConnection.connect = forbid_http
    http.client.HTTPSConnection.connect = forbid_http
    root = Path(cache_dir)
    payload = root / "models--openbmb--VoxCPM2" / "snapshots" / "fixture"
    payload.mkdir(parents=True)
    (payload / "weights.bin").write_bytes(f"{repo}@{revision}".encode("utf-8"))
    return {**calls, "fixture_bytes": (payload / "weights.bin").stat().st_size}


class TTSRuntimePackagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Archive/rollback fixtures intentionally contain no real interpreter.
        health = patch.object(rp, "check_runtime_health")
        health.start()
        self.addCleanup(health.stop)
        # 隔离统一仓库目录到临时路径
        self._old_root = rp.tts_root
        rp.tts_root = lambda: self.root / "appdata" / "tts"  # type: ignore[assignment]
        # 权重判定可控(本机真有 VoxCPM2 缓存,测试里压成未装)
        self._old_weights = rp.is_weights_installed
        rp.is_weights_installed = lambda model_id: False  # type: ignore[assignment]
        self._old_plat = rp.current_platform
        rp.current_platform = lambda: "mac-arm64"  # type: ignore[assignment]
        self._old_fetch = rp._fetch_manifest
        self._old_trusted = rp._trusted_manifest
        self._old_dl = rp._download
        self._old_extract = rp._safe_extract_runtime
        self._old_app_data_dir = rp.mm.app_data_dir
        rp.mm.app_data_dir = lambda: self.root / "appdata"  # type: ignore[assignment]
        self._old_local_model_dir = rp.local_models.model_dir
        self._old_hf_cache_dir = rp.local_models.hf_cache_dir
        rp.local_models.model_dir = (  # type: ignore[assignment]
            lambda model_id=rp.local_models.VOXCPM2.id:
            self.root / "models" / model_id
        )
        rp.local_models.hf_cache_dir = (  # type: ignore[assignment]
            lambda model_id=rp.local_models.VOXCPM2.id:
            rp.local_models.model_dir(model_id) / "hf-cache"
        )

    def tearDown(self):
        rp.tts_root = self._old_root  # type: ignore[assignment]
        rp.is_weights_installed = self._old_weights  # type: ignore[assignment]
        rp.current_platform = self._old_plat  # type: ignore[assignment]
        rp._fetch_manifest = self._old_fetch  # type: ignore[assignment]
        rp._trusted_manifest = self._old_trusted  # type: ignore[assignment]
        rp._download = self._old_dl  # type: ignore[assignment]
        rp._safe_extract_runtime = self._old_extract  # type: ignore[assignment]
        rp.mm.app_data_dir = self._old_app_data_dir  # type: ignore[assignment]
        rp.local_models.model_dir = self._old_local_model_dir  # type: ignore[assignment]
        rp.local_models.hf_cache_dir = self._old_hf_cache_dir  # type: ignore[assignment]
        self.tmp.cleanup()

    def _make_fake_pkg(self) -> tuple[Path, str]:
        """造一个假运行时包:顶层 python/bin/python3.11。"""
        stage = self.root / "stage" / "python" / "bin"
        stage.mkdir(parents=True)
        (stage / "python3.11").write_text("#!/bin/sh\necho fake\n")
        tar = self.root / "rt.tar.gz"
        with tarfile.open(tar, "w:gz") as t:
            t.add(self.root / "stage" / "python", arcname="python")
        return tar, rp._sha256(tar)

    def _set_test_manifest(self, manifest: dict) -> None:
        provider = lambda _spec, _plat: manifest
        rp._fetch_manifest = provider  # type: ignore[assignment]
        rp._trusted_manifest = provider  # type: ignore[assignment]


    def test_runtime_archive_download_resumes_partial_bytes(self):
        payload = b"0123456789"
        seen_headers = []

        class Response(io.BytesIO):
            def __init__(self, data, *, status, headers, fail_after_first=False):
                super().__init__(data)
                self.status = status
                self.headers = headers
                self.fail_after_first = fail_after_first
                self.reads = 0

            def read(self, size=-1):
                self.reads += 1
                if self.fail_after_first and self.reads > 1:
                    raise http.client.IncompleteRead(b"", 6)
                return super().read(4 if self.fail_after_first else size)

            def __enter__(self): return self
            def __exit__(self, *_args): self.close(); return False

        responses = [
            Response(payload, status=206, headers={"Content-Length": "10", "Content-Range": "bytes 0-9/10"}, fail_after_first=True),
            Response(payload[4:], status=206, headers={"Content-Length": "6", "Content-Range": "bytes 4-9/10"}),
        ]

        def open_fake(_url, _timeout, headers=None):
            seen_headers.append(dict(headers or {}))
            return responses.pop(0)

        destination = self.root / "runtime.tar.gz"
        with (
            patch.object(rp, "_open_runtime_url", side_effect=open_fake),
            patch.object(rp.time, "sleep", return_value=None),
        ):
            rp._download(
                "https://github.com/AidenXu-1/lulu-runtimes/releases/download/x/runtime.tar.gz",
                destination, lambda *_: None, lambda: False, "运行时", len(payload),
            )
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(seen_headers[1].get("Range"), "bytes=4-9")


    def test_runtime_archive_keeps_partial_after_bounded_retries_are_exhausted(self):
        payload = b"0123456789"

        class Response(io.BytesIO):
            def __init__(self, data, *, status, headers):
                super().__init__(data)
                self.status = status
                self.headers = headers

            def __enter__(self): return self
            def __exit__(self, *_args): self.close(); return False

        responses = [
            Response(payload[:4], status=206, headers={"Content-Length": "10", "Content-Range": "bytes 0-9/10"}),
            *[
                Response(b"", status=206, headers={"Content-Length": "6", "Content-Range": "bytes 4-9/10"})
                for _ in range(rp._RUNTIME_RANGE_ATTEMPTS - 1)
            ],
        ]
        destination = self.root / "runtime-still-interrupted.tar.gz"
        with (
            patch.object(rp, "_open_runtime_url", side_effect=lambda *_args, **_kwargs: responses.pop(0)),
            patch.object(rp.time, "sleep", return_value=None),
            self.assertRaisesRegex(RuntimeError, "已保留当前进度"),
        ):
            rp._download(
                "https://github.com/AidenXu-1/lulu-runtimes/releases/download/x/runtime.tar.gz",
                destination, lambda *_: None, lambda: False, "运行时", len(payload),
            )

        partials = list(destination.parent.glob(f".{destination.name}.lulu-resume"))
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0].read_bytes(), payload[:4])

    def test_runtime_archive_uses_small_verified_ranges_until_complete(self):
        payload = b"0123456789"
        requested = []

        class Response(io.BytesIO):
            def __init__(self, start, end):
                super().__init__(payload[start:end + 1])
                self.status = 206
                self.headers = {
                    "Content-Length": str(end - start + 1),
                    "Content-Range": f"bytes {start}-{end}/{len(payload)}",
                }

            def __enter__(self): return self
            def __exit__(self, *_args): self.close(); return False

        def open_fake(_url, _timeout, headers=None):
            value = (headers or {})["Range"]
            requested.append(value)
            start, end = (int(part) for part in value.removeprefix("bytes=").split("-"))
            return Response(start, end)

        destination = self.root / "runtime-ranges.tar.gz"
        with (
            patch.object(rp, "_RUNTIME_RANGE_BYTES", 4),
            patch.object(rp, "_open_runtime_url", side_effect=open_fake),
        ):
            rp._download(
                "https://github.com/AidenXu-1/lulu-runtimes/releases/download/x/runtime.tar.gz",
                destination, lambda *_: None, lambda: False, "运行时", len(payload),
            )

        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(requested, ["bytes=0-3", "bytes=4-7", "bytes=8-9"])

    def test_ignored_range_response_never_truncates_existing_partial(self):
        payload = b"0123456789"
        destination = self.root / "runtime-preserve.tar.gz"
        partial = destination.parent / f".{destination.name}.lulu-resume"
        partial.write_bytes(payload[:4])

        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Length": "10"}

            def __enter__(self): return self
            def __exit__(self, *_args): self.close(); return False

        with (
            patch.object(rp, "_open_runtime_url", side_effect=lambda *_args, **_kwargs: Response(payload)),
            patch.object(rp.time, "sleep", return_value=None),
            self.assertRaisesRegex(RuntimeError, "已保留当前进度"),
        ):
            rp._download(
                "https://github.com/AidenXu-1/lulu-runtimes/releases/download/x/runtime.tar.gz",
                destination, lambda *_: None, lambda: False, "运行时", len(payload),
            )

        self.assertEqual(partial.read_bytes(), payload[:4])


if __name__ == "__main__":
    unittest.main()
