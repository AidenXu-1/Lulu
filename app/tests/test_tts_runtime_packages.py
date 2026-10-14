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

    def test_runtime_repair_keeps_existing_weights_and_metadata_visible(self):
        root = rp.model_dir("local_voxcpm2")
        weight = root / "runtime" / "vendor" / "fixture-vendor" / "weight.bin"
        metadata = root / ".fixture_metadata"
        weight.parent.mkdir(parents=True)
        weight.write_bytes(b"trusted-existing-weight")
        metadata.write_text("ready", encoding="utf-8")
        observed = []

        def install_runtime(*_args, **_kwargs):
            observed.append((weight.exists(), metadata.exists()))

        def install_weights(*_args, **_kwargs):
            observed.append((weight.exists(), metadata.exists()))

        with patch.object(rp, "is_runtime_installed", return_value=False), patch.object(
            rp, "install_runtime", side_effect=install_runtime
        ), patch.object(rp, "install_weights", side_effect=install_weights):
            rp.install("local_voxcpm2")

        self.assertEqual(observed, [(True, True), (True, True)])
        self.assertEqual(weight.read_bytes(), b"trusted-existing-weight")
        self.assertTrue(metadata.exists())

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

    def test_runtime_archive_recovers_early_eof_inside_one_user_attempt(self):
        payload = b"0123456789"
        seen_headers = []

        class Response(io.BytesIO):
            def __init__(self, data, *, status, headers):
                super().__init__(data)
                self.status = status
                self.headers = headers

            def __enter__(self): return self
            def __exit__(self, *_args): self.close(); return False

        responses = [
            Response(payload[:4], status=206, headers={"Content-Length": "10", "Content-Range": "bytes 0-9/10"}),
            Response(payload[4:], status=206, headers={"Content-Length": "6", "Content-Range": "bytes 4-9/10"}),
        ]

        def open_fake(_url, _timeout, headers=None):
            seen_headers.append(dict(headers or {}))
            return responses.pop(0)

        destination = self.root / "runtime-auto-retry.tar.gz"
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

    def test_empty_runtime_cache_does_not_create_false_repair_state(self):
        empty = (
            rp.mm.app_data_dir() / "model-download-cache" / "tts-runtime"
            / "local_indextts2-empty"
        )
        empty.mkdir(parents=True)

        self.assertFalse(rp.has_existing_install_data("local_indextts2"))

    def test_runtime_cache_with_unsafe_permissions_fails_closed(self):
        root = rp.mm.app_data_dir() / "model-download-cache" / "tts-runtime"
        root.mkdir(parents=True)
        root.chmod(0o777)

        with self.assertRaisesRegex(RuntimeError, "权限异常"):
            rp.has_existing_install_data("local_indextts2")

    def test_uninstall_removes_only_that_models_revision_bound_runtime_cache(self):
        target = rp.mm.app_data_dir() / "model-download-cache" / "tts-runtime" / "local_indextts2-deadbeef"
        other = rp.mm.app_data_dir() / "model-download-cache" / "tts-runtime" / "local_voxcpm2-cafebabe"
        target.mkdir(parents=True)
        other.mkdir(parents=True)
        (target / ".runtime.tar.gz.lulu-resume").write_bytes(b"partial")
        (other / ".runtime.tar.gz.lulu-resume").write_bytes(b"keep")

        rp.uninstall("local_indextts2", remove_weights=True)

        self.assertFalse(target.exists())
        self.assertTrue(other.exists())

    def test_uninstall_unlinks_runtime_cache_symlink_without_touching_target(self):
        outside = self.root / "outside-cache"
        outside.mkdir()
        sentinel = outside / "keep.bin"
        sentinel.write_bytes(b"keep")
        link = rp.mm.app_data_dir() / "model-download-cache" / "tts-runtime" / "local_indextts2-linked"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside, target_is_directory=True)

        rp.uninstall("local_indextts2", remove_weights=True)

        self.assertFalse(os.path.lexists(link))
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_runtime_install_keeps_revision_bound_download_cache_between_retries(self):
        model_id = "local_voxcpm2"
        archive, sha = self._make_fake_pkg()
        self._set_test_manifest({
            "file": "rt.tar.gz",
            "sha256": sha,
            "size": archive.stat().st_size,
            "version": "test-resume",
        })
        destinations = []

        def download(_url, dest, _progress, _is_cancelled, _label, _expected_size=None):
            destinations.append(dest)
            partial = dest.parent / f".{dest.name}.lulu-resume"
            if len(destinations) == 1:
                partial.write_bytes(b"retained-partial")
                raise OSError("network interrupted")
            self.assertEqual(dest, destinations[0])
            self.assertEqual(partial.read_bytes(), b"retained-partial")
            shutil.copy(archive, dest)

        rp._download = download  # type: ignore[assignment]
        with self.assertRaisesRegex(OSError, "network interrupted"):
            rp.install_runtime(model_id)
        rp.install_runtime(model_id)

        self.assertTrue(rp.is_runtime_installed(model_id))
        self.assertEqual(destinations[0], destinations[1])
        self.assertFalse(destinations[0].parent.exists())

    def test_status_paths_install_uninstall(self):
        mid = "local_voxcpm2"
        self.assertEqual(rp.status(mid), "not_installed")
        self.assertFalse(rp.is_runtime_installed(mid))

        tar, sha = self._make_fake_pkg()
        self._set_test_manifest({"file": "rt.tar.gz", "sha256": sha, "size": tar.stat().st_size, "version": "test"})
        rp._download = lambda url, dest, progress, is_cancelled, label, expected_size=None: shutil.copy(tar, dest)  # type: ignore[assignment]

        events = []
        rp.install_runtime(mid, progress=lambda m, f: events.append((m, f)))

        self.assertTrue(rp.is_runtime_installed(mid))
        # 解释器路径在统一仓库下
        py = rp.runtime_python(mid)
        self.assertTrue(str(py).endswith("python/bin/python3.11"))
        self.assertIn("tts/local_voxcpm2/runtime", str(py).replace("\\", "/"))
        # 权重未装(测试压制)→ runtime_only
        self.assertEqual(rp.status(mid), "runtime_only")
        # state 文件写了
        self.assertTrue((rp.model_dir(mid) / rp._STATE).exists())
        self.assertTrue(events)

        rp.uninstall(mid)
        self.assertFalse(rp.is_runtime_installed(mid))
        self.assertEqual(rp.status(mid), "not_installed")

    def test_uninstall_unlinks_managed_symlink_without_touching_external_target(self):
        mid = "local_indextts2"
        outside = self.root / "outside-runtime"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        rp.tts_root().mkdir(parents=True)
        rp.model_dir(mid).symlink_to(outside, target_is_directory=True)

        rp.uninstall(mid, remove_weights=True)

        self.assertFalse(os.path.lexists(rp.model_dir(mid)))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_uninstall_propagates_filesystem_failure_instead_of_claiming_success(self):
        mid = "local_indextts2"
        rp.model_dir(mid).mkdir(parents=True)
        (rp.model_dir(mid) / "weight.bin").write_bytes(b"x")

        with patch.object(rp.shutil, "rmtree", side_effect=OSError("permission denied")):
            with self.assertRaisesRegex(OSError, "permission denied"):
                rp.uninstall(mid, remove_weights=True)

        self.assertTrue(rp.model_dir(mid).exists())

    def test_attacker_manifest_and_matching_hash_cannot_replace_app_lock(self):
        attacker = {
            "model_id": "local_voxcpm2",
            "platform": "mac-arm64",
            "version": "attacker",
            "file": "attacker.tar.gz",
            "sha256": "a" * 64,
            "size": 128,
        }
        rp._fetch_manifest = lambda _spec, _plat: attacker  # type: ignore[assignment]
        with patch.object(rp, "_download") as download, self.assertRaisesRegex(RuntimeError, "可信锁"):
            rp.install_runtime("local_voxcpm2")
        download.assert_not_called()

    def test_existing_interpreter_and_matching_legacy_state_without_receipt_are_not_trusted(self):
        mid = "local_voxcpm2"
        interpreter = rp.runtime_dir(mid) / "python" / "bin" / "python3.11"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("placeholder", encoding="utf-8")
        manifest = self._old_trusted(rp.TTS_RUNTIMES[mid], "mac-arm64")
        (rp.model_dir(mid) / rp._STATE).write_text(
            __import__("json").dumps({
                "platform": "mac-arm64",
                "version": manifest["version"],
                "sha256": manifest["sha256"],
            }),
            encoding="utf-8",
        )
        self.assertFalse(rp.is_runtime_installed(mid))
        self.assertTrue(rp.has_existing_install_data(mid))
        self.assertEqual(rp.status(mid), "needs_repair")

    def test_failed_runtime_repair_preserves_existing_untrusted_runtime(self):
        mid = "local_voxcpm2"
        interpreter = rp.runtime_dir(mid) / "python" / "bin" / "python3.11"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("old-runtime", encoding="utf-8")
        tar, _sha = self._make_fake_pkg()
        self._set_test_manifest({"file": "rt.tar.gz", "sha256": "0" * 64, "size": tar.stat().st_size, "version": "x"})
        rp._download = lambda _url, dest, *_args: shutil.copy(tar, dest)  # type: ignore[assignment]

        with self.assertRaisesRegex(RuntimeError, "校验失败"):
            rp.install_runtime(mid)

        self.assertEqual(interpreter.read_text(encoding="utf-8"), "old-runtime")

    def test_complete_install_restores_whole_previous_model_when_weights_fail(self):
        mid = "local_voxcpm2"
        old = rp.model_dir(mid)
        old.mkdir(parents=True)
        sentinel = old / "existing-weights.bin"
        sentinel.write_bytes(b"keep-complete-model")

        def fake_runtime(_model_id, _progress, _cancelled):
            new_runtime = rp.runtime_dir(mid) / "python" / "bin"
            new_runtime.mkdir(parents=True)
            (new_runtime / "python3.11").write_text("new", encoding="utf-8")

        with (
            patch.object(rp, "install_runtime", side_effect=fake_runtime),
            patch.object(rp, "install_weights", side_effect=RuntimeError("weights failed")),
            self.assertRaisesRegex(RuntimeError, "weights failed"),
        ):
            rp.install(mid)

        self.assertEqual(sentinel.read_bytes(), b"keep-complete-model")
        self.assertEqual(list(rp.tts_root().glob(f".{mid}-repair-*")), [])

    def test_install_rejects_bad_checksum(self):
        mid = "local_voxcpm2"
        tar, _ = self._make_fake_pkg()
        self._set_test_manifest({"file": "rt.tar.gz", "sha256": "deadbeef", "size": tar.stat().st_size, "version": "x"})
        rp._download = lambda url, dest, progress, is_cancelled, label, expected_size=None: shutil.copy(tar, dest)  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            rp.install_runtime(mid)
        self.assertFalse(rp.is_runtime_installed(mid))  # 失败回滚

    def test_cancel_rolls_back(self):
        mid = "local_voxcpm2"
        tar, sha = self._make_fake_pkg()
        download_called = False
        self._set_test_manifest({"file": "rt.tar.gz", "sha256": sha, "size": tar.stat().st_size, "version": "x"})

        def _cancel_dl(url, dest, progress, is_cancelled, label, expected_size=None):
            nonlocal download_called
            download_called = True
            raise rp.CancelledError()
        rp._download = _cancel_dl  # type: ignore[assignment]
        with self.assertRaises(rp.CancelledError):
            rp.install_runtime(mid, is_cancelled=lambda: True)
        self.assertFalse(download_called)
        self.assertFalse(rp.runtime_dir(mid).exists())

    def test_install_rejects_path_escape_and_rolls_back(self):
        mid = "local_voxcpm2"
        tar_path = self.root / "escape.tar.gz"
        with tarfile.open(tar_path, "w:gz") as archive:
            info = tarfile.TarInfo("../escape.txt")
            payload = b"escape"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        sha = rp._sha256(tar_path)
        self._set_test_manifest({"file": tar_path.name, "sha256": sha, "size": tar_path.stat().st_size, "version": "x"})
        rp._download = lambda url, dest, progress, is_cancelled, label, expected_size=None: shutil.copy(tar_path, dest)  # type: ignore[assignment]

        with self.assertRaisesRegex(RuntimeError, "不安全路径"):
            rp.install_runtime(mid)

        self.assertFalse((self.root / "appdata" / "tts" / "escape.txt").exists())
        self.assertFalse(rp.runtime_dir(mid).exists())
        self.assertFalse(rp.runtime_dir(mid).with_name("runtime.installing").exists())
        self.assertFalse((rp.model_dir(mid) / rp._STATE).exists())

    def test_mid_extract_exception_removes_partial_runtime(self):
        mid = "local_voxcpm2"
        tar_path, sha = self._make_fake_pkg()
        self._set_test_manifest({"file": tar_path.name, "sha256": sha, "size": tar_path.stat().st_size, "version": "x"})
        rp._download = lambda url, dest, progress, is_cancelled, label, expected_size=None: shutil.copy(tar_path, dest)  # type: ignore[assignment]

        def _fail_midway(_tar, dest, _is_cancelled):
            partial = dest / "python" / "bin"
            partial.mkdir(parents=True)
            (partial / "python3.11").write_text("partial")
            raise OSError("disk full")

        rp._safe_extract_runtime = _fail_midway  # type: ignore[assignment]
        with self.assertRaisesRegex(OSError, "disk full"):
            rp.install_runtime(mid)

        self.assertFalse(rp.is_runtime_installed(mid))
        self.assertFalse(rp.runtime_dir(mid).exists())
        self.assertFalse(rp.runtime_dir(mid).with_name("runtime.installing").exists())
        self.assertFalse((rp.model_dir(mid) / rp._STATE).exists())

    def test_cancel_during_extract_removes_staging_runtime(self):
        mid = "local_voxcpm2"
        tar_path, sha = self._make_fake_pkg()
        cancelled = False
        self._set_test_manifest({"file": tar_path.name, "sha256": sha, "size": tar_path.stat().st_size, "version": "x"})

        def _download_then_cancel(url, dest, progress, is_cancelled, label, expected_size=None):
            nonlocal cancelled
            shutil.copy(tar_path, dest)
            cancelled = True

        rp._download = _download_then_cancel  # type: ignore[assignment]

        with self.assertRaises(rp.CancelledError):
            rp.install_runtime(mid, is_cancelled=lambda: cancelled)

        self.assertFalse(rp.runtime_dir(mid).exists())
        self.assertFalse(rp.runtime_dir(mid).with_name("runtime.installing").exists())
        self.assertFalse((rp.model_dir(mid) / rp._STATE).exists())

    def test_runtime_download_cancel_removes_operation_part(self):
        cancelled = False

        class Response:
            headers = {"Content-Length": "8"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                nonlocal cancelled
                if cancelled:
                    return b""
                cancelled = True
                return b"partial"

        dest = self.root / "runtime.tar.gz"
        with patch.object(rp, "_open_runtime_url", return_value=Response()):
            with self.assertRaises(rp.CancelledError):
                rp._download(
                    "https://fixture.invalid/runtime",
                    dest,
                    lambda *_args: None,
                    lambda: cancelled,
                    "fixture",
                )

        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_suffix(dest.suffix + ".part").exists())

    def test_runtime_redirect_policy_rejects_private_and_unlisted_targets(self):
        for target in ("https://127.0.0.1/internal", "https://evil.example/runtime.tar.gz", "http://github.com/runtime.tar.gz"):
            with self.subTest(target=target), self.assertRaisesRegex(RuntimeError, "地址"):
                rp._validate_runtime_url(target, redirect=True)

    def test_voxcpm2_receipt_survives_transaction_backup_cleanup(self):
        import json
        from src.core import artifact_trust as at
        cache = rp.local_models.hf_cache_dir()
        cache.mkdir(parents=True)
        (cache / "old.bin").write_bytes(b"old")
        def fake_download(repo, revision, target, cancel):
            snapshot = target / "models--openbmb--VoxCPM2" / "snapshots" / revision
            snapshot.mkdir(parents=True)
            (snapshot / "weights.bin").write_bytes(b"new")
        with patch.object(rp, "_snapshot_download_cancellable", side_effect=fake_download):
            rp.install_weights("local_voxcpm2")
        self.assertTrue(rp.local_models.is_downloaded())
        receipt = at._receipt_path(rp.mm.app_data_dir()/".artifact-trust", "tts-weights:local_voxcpm2")
        entries = json.loads(receipt.read_text())["files"]
        self.assertFalse(any(f["path"].startswith(".weights-attempt-") for f in entries))

    def test_voxcpm2_pre_cancel_never_calls_download_or_writes_ready(self):
        marker = rp.local_models.model_dir() / rp.local_models._DONE
        with patch.object(rp, "_snapshot_download_cancellable") as download:
            with self.assertRaises(rp.CancelledError):
                rp.install_weights("local_voxcpm2", is_cancelled=lambda: True)

        download.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertEqual(
            list(rp.local_models.model_dir().glob(".weights-attempt-*")),
            [],
        )

    def test_voxcpm2_mid_cancel_preserves_candidate_and_existing_cache(self):
        model_root = rp.local_models.model_dir()
        cache = rp.local_models.hf_cache_dir()
        cache.mkdir(parents=True)
        sentinel = cache / "existing.bin"
        sentinel.write_bytes(b"existing-cache")
        cancelled = False

        def fake_download(_repo, _revision, cache_dir, _is_cancelled):
            nonlocal cancelled
            attempt = Path(cache_dir)
            payload = attempt / "models--openbmb--VoxCPM2" / "snapshots" / "fixture"
            payload.mkdir(parents=True)
            (payload / "weights.bin").write_bytes(b"new")
            cancelled = True

        with patch.object(rp, "_snapshot_download_cancellable", side_effect=fake_download):
            with self.assertRaises(rp.CancelledError):
                rp.install_weights(
                    "local_voxcpm2",
                    is_cancelled=lambda: cancelled,
                )

        self.assertEqual(sentinel.read_bytes(), b"existing-cache")
        self.assertFalse((model_root / rp.local_models._DONE).exists())
        self.assertEqual(list(model_root.glob(".weights-attempt-*")), [])
        kept = list(model_root.glob(".weights-download-*/models--openbmb--VoxCPM2/snapshots/fixture/weights.bin"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), b"new")

    def test_voxcpm2_success_promotes_attempt_and_removes_previous_cache(self):
        model_root = rp.local_models.model_dir()
        cache = rp.local_models.hf_cache_dir()
        cache.mkdir(parents=True)
        (cache / "old.bin").write_bytes(b"old")

        def fake_download(_repo, _revision, cache_dir, _is_cancelled):
            attempt = Path(cache_dir)
            payload = attempt / "models--openbmb--VoxCPM2" / "snapshots" / "fixture"
            payload.mkdir(parents=True)
            (payload / "weights.bin").write_bytes(b"new")

        with patch.object(rp, "_snapshot_download_cancellable", side_effect=fake_download):
            rp.install_weights("local_voxcpm2")

        self.assertFalse((cache / "old.bin").exists())
        self.assertEqual(
            (
                cache
                / "models--openbmb--VoxCPM2"
                / "snapshots"
                / "fixture"
                / "weights.bin"
            ).read_bytes(),
            b"new",
        )
        self.assertTrue((model_root / rp.local_models._DONE).exists())
        self.assertEqual(list(model_root.glob(".weights-attempt-*")), [])

    def test_voxcpm2_cleanup_failure_fails_closed_and_preserves_old_cache(self):
        model_root = rp.local_models.model_dir()
        cache = rp.local_models.hf_cache_dir()
        cache.mkdir(parents=True)
        sentinel = cache / "existing.bin"
        sentinel.write_bytes(b"existing-cache")
        cancelled = False

        def fake_download(_repo, _revision, cache_dir, _is_cancelled):
            nonlocal cancelled
            attempt = Path(cache_dir)
            attempt.mkdir(parents=True, exist_ok=True)
            (attempt / "new.bin").write_bytes(b"new-cache")
            cancelled = True

        with (
            patch.object(rp, "_snapshot_download_cancellable", side_effect=fake_download),
            patch.object(rp.shutil, "rmtree", side_effect=OSError("fixture cleanup failure")),
            self.assertRaisesRegex(RuntimeError, "事务清理失败"),
        ):
            rp.install_weights(
                "local_voxcpm2",
                is_cancelled=lambda: cancelled,
            )

        self.assertEqual(sentinel.read_bytes(), b"existing-cache")
        self.assertFalse((model_root / rp.local_models._DONE).exists())
        attempts = list(model_root.glob(".weights-attempt-*"))
        self.assertEqual(len(attempts), 1)
        candidate = next(model_root.glob(".weights-download-*"))
        self.assertEqual((candidate / "new.bin").read_bytes(), b"new-cache")

    def test_spawn_snapshot_fixture_has_process_level_network_guards(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        cache = self.root / "spawn-cache"
        process = context.Process(
            target=rp._snapshot_download_child,
            args=(
                "openbmb/VoxCPM2",
                rp.HF_MODEL_REVISIONS["openbmb/VoxCPM2"],
                str(cache),
                child,
                _spawn_guarded_fake_snapshot,
            ),
        )
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(5), "spawn fixture produced no receipt")
            status, receipt = parent.recv()
        finally:
            parent.close()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)

        self.assertEqual(process.exitcode, 0)
        self.assertEqual(status, "ok")
        self.assertEqual(receipt["dns"], 0)
        self.assertEqual(receipt["socket"], 0)
        self.assertEqual(receipt["http"], 0)
        self.assertGreater(receipt["fixture_bytes"], 0)
        self.assertTrue(
            (
                cache
                / "models--openbmb--VoxCPM2"
                / "snapshots"
                / "fixture"
                / "weights.bin"
            ).is_file()
        )



if __name__ == "__main__":
    unittest.main()
