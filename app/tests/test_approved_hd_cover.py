"""Exercise the visible collection menu and real cover workflow with isolated files."""
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import QApplication

from src.config import AppConfig
from src.core.collect.cover import CoverLocalizationService
from src.core.collect.cover_workflow import CoverWorkflowService
from src.core.collect.history import CollectionHistoryRepository
from src.core.collect.models import from_aweme
from src.core.collect.repository import CollectionRepository
from src.ui.approved_collection import ApprovedCollection
from tests.test_collection_p3c_cover import FakePlan, FakeResolver, FakeTransport
from tests.test_douyin_hd_cover import aweme, image


class ApprovedHDCoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.patches = [
            patch('src.core.models_manager.app_data_dir', return_value=self.root),
            patch('src.core.library.LIBRARY_DIR', self.root / 'library'),
            patch('src.core.library.FOLDERS_FILE', self.root / 'library/folders.json'),
            patch('src.ui.collection_page.is_logged_in', side_effect=AssertionError('account access forbidden')),
            patch('src.ui.collection_page._fetch_managed_media', side_effect=AssertionError('media download forbidden')),
        ]
        for item in self.patches:
            item.start()
        self.pages = []
        self.small, self.full = image(360, 481), image(959, 1280, 'WEBP')
        self.transport = FakeTransport(FakePlan(body=self.small))
        self.localizer = CoverLocalizationService(self.root / 'collection/covers',
            transport=self.transport, resolver=FakeResolver())
        self.fresh = replace(from_aweme(aweme()), title='作者封面')
        receipt = self.localizer.localize(platform='douyin', work_id=self.fresh.work_id,
                                          url=self.fresh.cover_url)
        self.assertEqual(receipt.status, 'downloaded')
        self.cached = self.localizer.root / receipt.relative_path
        self.old = replace(self.fresh, cover_variants=(), cover_status='downloaded',
                           cover_local_path=receipt.relative_path)
        self.exported = self.root / 'already-exported.jpg'
        self.exported.write_bytes(self.small)
        self.records = CollectionRepository(self.root / 'collection/records.json')
        self.records.upsert([self.old])
        self.history = CollectionHistoryRepository(self.root / 'collection/history.json')
        self.history.add_record([self.old], platform='douyin',
            homepage_url='https://www.douyin.com/user/fixture', source_method='anonymous_latest',
            completion='complete')
        self.cfg = AppConfig(auto_update_enabled=False, collect_cover=False, keep_media=False)
        self.page = self.new_page()

    def tearDown(self):
        for page in reversed(self.pages):
            page.shutdown_workers()
            page.hide()
            page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def until(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail('isolated collection menu did not settle')

    def js(self, code):
        replies = []
        self.page.approved.page().runJavaScript(code, replies.append)
        self.until(lambda: bool(replies))
        return replies[0]

    def new_page(self):
        page = ApprovedCollection(self.cfg, history_repository=self.history,
            records_repository=self.records, cover_workflow=CoverWorkflowService(self.localizer),
            douyin_login_state=lambda: ('未登录', False))
        self.pages.append(page)
        page.resize(1200, 760)
        page.show()
        self.until(lambda: page.approved.bridge.is_ready)
        previous = getattr(self, 'page', None)
        self.page = page
        self.until(lambda: self.js('document.querySelectorAll("#worksBody tr").length') == 1)
        self.page = previous
        return page

    def open_menu(self, kind='cover', *, bottom=False):
        self.js("document.querySelectorAll('[popover]:popover-open').forEach(p=>p.hidePopover());"
                "document.querySelector('[data-asset-cell=\"" + kind + "\"]').dispatchEvent("
                "new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:100,clientY:"
                + ('innerHeight-1' if bottom else '100') + "}));void 0")

    def bridge(self, token, **arguments):
        replies = []
        self.page.approved.bridge.replied.connect(lambda key, raw: replies.append(json.loads(raw))
                                                if key == token else None)
        self.page.approved.bridge.request(token, 'redownload_cover', json.dumps(arguments))
        self.assertEqual(len(replies), 1)
        return replies[0]

    def exercise_download(self, *, fail=False):
        self.transport.plans.extend([FakePlan(status=403), FakePlan(status=403)] if fail else
            [FakePlan(body=self.small), FakePlan(body=self.full, mime='image/webp')])
        before = self.history.path.read_bytes()
        preferences = (self.cfg.collect_cover, self.cfg.keep_media, self.cfg.link_media_mode)
        with patch('src.core.collect.douyin_fetch.fetch_item_with_page_fallback', return_value=self.fresh) as fetch:
            self.open_menu(bottom=True)
            self.assertTrue(self.js('!!document.querySelector("#redownloadCover:not([hidden])")'))
            self.assertEqual(self.js('document.querySelector("#redownloadCover").textContent'), '重新下载高清封面')
            self.assertTrue(self.js('document.querySelector("#coverMenu").getBoundingClientRect().bottom<=innerHeight-10'))
            self.js('document.querySelector("#redownloadCover").click()')
            self.until(lambda: self.page._cover_retry_worker is None and fetch.called)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[0], self.old.work_id)
        self.assertEqual(self.history.path.read_bytes(), before)
        self.assertEqual(self.exported.read_bytes(), self.small)
        self.assertEqual(preferences, (self.cfg.collect_cover, self.cfg.keep_media, self.cfg.link_media_mode))
        return self.page._managed_cover_file(self.page._items[0])

    def test_visible_menu_downloads_hd_and_reopen_preserves_cached_result(self):
        path = self.exercise_download()
        reader = QImageReader(str(path))
        self.assertEqual(reader.size().toTuple(), (959, 1280))
        self.assertEqual(bytes(reader.format()), b'webp')
        self.assertEqual(path.read_bytes(), self.full)
        self.page = self.new_page()
        self.assertEqual(self.page._managed_cover_file(self.page._items[0]), path)
        self.assertEqual(len(self.transport.calls), 3)

    def test_failed_redownload_keeps_old_cover_and_exported_file(self):
        path = self.exercise_download(fail=True)
        self.assertEqual(path, self.cached)
        self.assertEqual(path.read_bytes(), self.small)
        self.assertIn('保留原封面 1 条', self.page.status.text())

    def test_missing_url_can_refresh_only_the_verified_same_work(self):
        self.old = replace(self.old, cover_url='')
        self.page._items = [self.old]
        self.page._render()
        self.page._publish(force=True)
        self.assertEqual(self.exercise_download().read_bytes(), self.full)

    def test_non_douyin_cover_and_media_have_only_finder_action(self):
        with patch.object(self.page, '_managed_media_file', return_value=self.cached):
            self.page._publish(force=True)
            self.open_menu('media')
            self.assertFalse(self.js('!!document.querySelector("#redownloadCover:not([hidden])")'))
        self.page._items = [replace(self.old, aweme_id='', platform='bilibili', work_id='BVfixture',
                                    work_url='https://www.bilibili.com/video/BVfixture')]
        self.page._render()
        self.page._publish(force=True)
        self.open_menu()
        self.assertFalse(self.js('!!document.querySelector("#redownloadCover:not([hidden])")'))
        self.assertTrue(self.js('document.querySelector("#coverMenu").matches(":popover-open")'))

    def test_bridge_rechecks_identity_missing_file_and_busy_state(self):
        with patch.object(self.page, '_retry_cover_item') as retry:
            self.assertFalse(self.bridge('101', id='douyin:wrong')['ok'])
            moved = self.root / 'moved.jpg'
            self.cached.rename(moved)
            self.assertFalse(self.bridge('102', id=self.old.stable_key)['ok'])
            moved.rename(self.cached)
            with patch.object(self.page, '_cover_retry_worker', SimpleNamespace(_items=(self.old,))):
                self.assertFalse(self.bridge('103', id=self.old.stable_key)['ok'])
            self.page._items = [replace(self.old, aweme_id='', platform='bilibili', work_id='BVfixture',
                                        work_url='https://www.bilibili.com/video/BVfixture')]
            self.assertFalse(self.bridge('104', id=self.page._items[0].stable_key)['ok'])
            retry.assert_not_called()

    def test_menu_uses_current_material_state_after_refresh(self):
        self.open_menu()
        self.assertTrue(self.js('!!document.querySelector("#redownloadCover:not([hidden])")'))
        self.page._set_material_runtime(self.old.stable_key, 'cover', 'downloading')
        self.page._works_dirty = True
        self.page._publish(force=True)
        self.until(lambda: self.js('!!document.querySelector("[data-asset-cell=cover] .material-progress")'))
        self.open_menu()
        self.assertFalse(self.js('document.querySelector("#coverMenu").matches(":popover-open")'))
