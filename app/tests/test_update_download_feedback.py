"""Real download service with delayed in-memory HTTP, temporary resume bytes and Qt buttons."""
import hashlib
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from tests.test_auto_update import _raw_manifest
from tests.test_auto_update_ui import Config
from src.core.updater.manifest import parse_manifest
from src.core.updater.models import UpdateError
from src.core.updater.network import Response
from src.core.updater.secure_io import atomic_json
from src.core.updater.service import UpdateService
from src.ui.update_ui import AboutUpdatePanel, UpdateController, UpdatePanel


class DownloadFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.body = b"isolated complete download"
        self.manifest = parse_manifest(_raw_manifest(zip_sha=hashlib.sha256(self.body).hexdigest(), zip_size=len(self.body)), local_channel="trusted-test")

    def settle(self, controller):
        deadline = time.monotonic() + 5
        while controller._worker is not None and time.monotonic() < deadline:
            loop = QEventLoop(); QTimer.singleShot(10, loop.quit); loop.exec()
        self.assertIsNone(controller._worker)

    def prepare(self, client, *, about=True, offset=0, directory="updates"):
        service = UpdateService(root=self.root / directory, client=client, public_keys={"test": "fixture"})
        service.manifest = self.manifest
        service._save(state="download_paused" if offset else "available", ui_state="download_paused" if offset else "update_available",
            verified_manifest=True, latest_version=self.manifest.latest_version, latest_build=self.manifest.latest_build,
            downloaded=offset, total=len(self.body))
        if offset:
            asset = self.manifest.zip_asset
            self.part = service.store.root / "downloads" / (asset.filename + ".part")
            self.part.write_bytes(self.body[:offset]); self.part.chmod(0o600)
            atomic_json(self.part.with_name(self.part.name + ".meta.json"), {
                "url":asset.url, "version":self.manifest.latest_version, "build":self.manifest.latest_build,
                "sha256":asset.sha256, "expected_size":asset.size, "filename":asset.filename,
                "downloaded_bytes":offset, "etag":"fixture", "txn":"a"*64})
        controller = UpdateController(service=service); controller._result_timer.stop()
        panel = (AboutUpdatePanel(Config(), controller, repository_url="repo", author_url="author",
                 product_boundary="fixture", open_url=lambda _: None, show_contact=lambda: None)
                 if about else UpdatePanel(Config(), controller))
        self.addCleanup(panel.close)
        return service, controller, panel

    def slow_client(self, *, phase="open", offset=0, failure=""):
        entered, release = threading.Event(), threading.Event()
        body = self.body
        def block():
            entered.set()
            if not release.wait(5): raise UpdateError("timeout", "fixture timeout")
            if failure: raise UpdateError(failure, "fixture connection failed")
        class Stream(BytesIO):
            def read(self, size=-1):
                if phase == "read": block()
                return super().read(size)
        class Client:
            def __init__(self): self.requests = []
            def open(self, url, *, headers, **kwargs):
                self.requests.append(dict(headers))
                if phase == "open": block()
                response_headers = {"etag":"fixture", "content-length":str(len(body)-offset)}
                if offset: response_headers["content-range"] = f"bytes {offset}-{len(body)-1}/{len(body)}"
                return Response(url, 206 if offset else 200, response_headers, Stream(body[offset:]))
        return Client(), entered, release

    def test_slow_connection_and_first_chunk_immediately_show_download_in_both_panels(self):
        for about in (False, True):
            for phase in ("open", "read"):
                with self.subTest(about=about, phase=phase):
                    client, entered, release = self.slow_client(phase=phase)
                    service, controller, panel = self.prepare(client, about=about, directory=f"{about}-{phase}")
                    try:
                        with patch.object(service, "stage", side_effect=lambda path: service._save(state="ready_to_install", ui_state="ready")):
                            panel.primary.click()
                            self.assertEqual(panel.primary.text(), "暂停下载")
                            self.assertTrue(entered.wait(1))
                            self.app.processEvents()
                            self.assertEqual(panel.primary.text(), "暂停下载")
                            controller.download(); controller.check(manual=True)
                            self.assertEqual(len(client.requests), 1)
                            release.set(); self.settle(controller)
                        self.assertEqual(controller.snapshot.ui_state, "ready")
                        self.assertEqual((service.store.root / "downloads" / self.manifest.zip_asset.filename).read_bytes(), self.body)
                    finally:
                        release.set(); controller.shutdown(3000); self.app.processEvents()

    def test_pause_waits_honestly_and_reopen_resumes_exact_existing_bytes(self):
        self._assert_pause_and_reopen(about=True)

    def test_compact_panel_pause_waits_honestly_and_reopens(self):
        self._assert_pause_and_reopen(about=False)

    def _assert_pause_and_reopen(self, *, about):
        client, entered, release = self.slow_client(offset=4)
        service, controller, panel = self.prepare(client, offset=4, about=about)
        try:
            with patch.object(service, "stage") as stage:
                panel.primary.click()
                self.assertTrue(entered.wait(1))
                self.assertEqual(panel.primary.text(), "暂停下载")
                panel.primary.click()
                self.assertEqual(panel.primary.text(), "正在暂停…")
                self.assertFalse(panel.primary.isEnabled())
                panel.primary.click()
                self.assertEqual(self.part.read_bytes(), self.body[:4])
                release.set(); self.settle(controller)
                stage.assert_not_called()
            self.assertEqual(controller.snapshot.ui_state, "download_paused")
            self.assertEqual(panel.primary.text(), "继续下载")
            self.assertEqual(self.part.read_bytes(), self.body[:4])
            resume_client, _, resume_release = self.slow_client(offset=4); resume_release.set()
            reopened = UpdateService(root=service.store.root, client=resume_client, public_keys={})
            self.assertEqual(reopened.snapshot.downloaded, 4)
            # Isolated manifest fixture stands in for the already-tested signed check.
            reopened.manifest = self.manifest
            result = reopened.download()
            self.assertEqual(resume_client.requests[0]["Range"], "bytes=4-")
            self.assertEqual(result.read_bytes(), self.body)
            self.assertFalse(self.part.exists())
        finally:
            release.set(); controller.shutdown(3000); self.app.processEvents()

    def test_connection_failure_restores_retry_without_losing_partial(self):
        client, entered, release = self.slow_client(offset=4, failure="timeout")
        service, controller, panel = self.prepare(client, offset=4)
        try:
            panel.primary.click(); self.assertTrue(entered.wait(1))
            self.assertEqual(panel.primary.text(), "暂停下载")
            release.set(); self.settle(controller)
            self.assertEqual(controller.snapshot.ui_state, "download_paused")
            self.assertTrue(panel.primary.isEnabled())
            self.assertEqual(panel.primary.text(), "继续下载")
            self.assertEqual(self.part.read_bytes(), self.body[:4])
        finally:
            release.set(); controller.shutdown(3000); self.app.processEvents()

    def test_pause_during_recheck_never_starts_zip_request(self):
        service, controller, panel = self.prepare(Mock(), offset=4)
        service.manifest = None
        entered, release = threading.Event(), threading.Event()
        def check(*, manual):
            entered.set()
            if not release.wait(5): raise UpdateError("timeout", "fixture timeout")
            service.manifest = self.manifest
            return service._save(ui_state="download_paused", verified_manifest=True)
        try:
            with patch.object(service, "check", side_effect=check):
                panel.primary.click(); self.assertTrue(entered.wait(1))
                panel.primary.click(); release.set(); self.settle(controller)
            service.client.open.assert_not_called()
            self.assertEqual(controller.snapshot.ui_state, "download_paused")
            self.assertEqual(self.part.read_bytes(), self.body[:4])
        finally:
            release.set(); controller.shutdown(3000); self.app.processEvents()
