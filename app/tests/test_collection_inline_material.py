"""Isolated row actions: no real accounts, services, media or system viewers."""
from __future__ import annotations
import base64
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from src.core.collect.application import CollectionApplicationService
from src.core.collect.cover import CoverReceipt
from src.core.collect.cover_workflow import CoverWorkflowService
from src.core.collect.models import CollectedItem
from src.core.collect.repository import CollectionRepository
from src.ui import collection_page
from src.ui.collection_page import CollectionPage

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')

class Localizer:
    startup_recovery_receipts = ()
    def __init__(self, root): self.root, self.calls = root, []
    def localize(self, *, platform, work_id, url, is_cancelled=None):
        self.calls.append(work_id)
        relative = platform + '/' + hashlib.sha256((platform + ':' + work_id).encode()).hexdigest()[:32] + '.png'
        target = self.root / 'collection' / 'covers' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(PNG)
        return CoverReceipt('downloaded', relative_path=relative, size=len(PNG), sha256=hashlib.sha256(PNG).hexdigest(), mime_type='image/png')

class InlineMaterialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.repo = CollectionRepository(self.root / 'collection' / 'records.json')
        self.cfg = SimpleNamespace(collect_cover=False, link_media_mode='audio', keep_media=False,
            feishu_target={}, feishu_creation_target={}, feishu_table_targets=[], feishu_storage_preferences={},
            last_output_dir=str(self.root / 'out'), media_dir=str(self.root / 'media'), save=lambda: True)
        self.patches = [patch('src.ui.collection_page.is_logged_in', return_value=False),
                        patch('src.core.models_manager.app_data_dir', return_value=self.root),
                        patch('src.core.library.LIBRARY_DIR', self.root / 'library'),
                        patch('src.core.library.FOLDERS_FILE', self.root / 'library' / 'folders.json'),
                        patch('socket.create_connection', side_effect=AssertionError('network forbidden')),
                        patch('src.ui.collection_page.QDesktopServices.openUrl', return_value=True),
                        patch('src.ui.collection_page.subprocess.run', side_effect=AssertionError('system effects forbidden'))]
        self.started_patches = [p.start() for p in self.patches]
        self.localizer = Localizer(self.root)
        self.page = CollectionPage(self.cfg, collection_service=CollectionApplicationService(
            adapter_resolver=lambda _: (_ for _ in ()).throw(AssertionError('adapter forbidden')),
            session_factory=lambda *_: (_ for _ in ()).throw(AssertionError('browser forbidden'))),
            records_repository=self.repo, cover_workflow=CoverWorkflowService(self.localizer))
        self.page.resize(1680, 960); self.calls = []; self.release = None
        self.items = [CollectedItem('100', author_name='作者甲', title='第一条', like=1,
                      work_url='https://www.douyin.com/video/100', play_url='https://v26-weba.douyinvod.com/video/100.mp4',
                      cover_url='https://p3-pc-sign.douyinpic.com/100.png'),
                      CollectedItem('101', author_name='作者乙', title='第二条', like=10,
                      work_url='https://www.douyin.com/video/101', play_url='https://v26-weba.douyinvod.com/video/101.mp4',
                      cover_url='https://p3-pc-sign.douyinpic.com/101.png')]
        self.repo.upsert(self.items); self.page._set_items(self.items)
    def tearDown(self):
        if self.release: self.release.set()
        self.page.shutdown_workers(); self.page.close(); self.page.deleteLater(); self.app.processEvents()
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()
    def wait(self, predicate, timeout=3):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.app.processEvents()
            if predicate(): return
            time.sleep(.002)
        self.fail('isolated operation timed out')
    def done(self): self.wait(lambda: self.page._material_worker is None and not self.page._pending_material_jobs)
    def cell(self, row=0, kind='media'): return self.page.table.cellWidget(row, 10 if kind=='media' else 11)
    def fetch(self, item, mode, destination, cancel, *, on_progress=None):
        self.calls.append((item.stable_key,mode))
        if on_progress: on_progress('fixed fixture message',.25)
        if self.release:
            if not self.release.wait(2): raise AssertionError('test gate not released')
        result=Path(destination)/'fixture.mp4';result.write_bytes(b'isolated media fixture');return result

    def test_cover_without_address_checks_exact_work_on_click_and_can_retry(self):
        item = CollectedItem('102', title='缺少封面地址', work_url='https://www.douyin.com/video/102')
        resolved = replace(item, cover_url=self.items[0].cover_url)
        self.repo.upsert([item])
        self.page._set_items([item])
        self.assertEqual(self.cell(kind='cover').property('materialState'), 'missing')
        with patch('src.core.collect.douyin_fetch.fetch_item_with_page_fallback',
                   side_effect=[self.items[1], resolved]) as lookup:
            self.cell(kind='cover').button.click()
            self.done()
            self.assertEqual(self.cell(kind='cover').property('materialState'), 'failed')
            self.assertEqual(self.localizer.calls, [])
            self.cell(kind='cover').button.click()
            self.done()
        self.assertEqual([call.args[0] for call in lookup.call_args_list], [item.work_id] * 2)
        self.assertEqual(self.localizer.calls, [item.work_id])
        self.assertEqual(self.cell(kind='cover').property('materialState'), 'ready')
        saved = next(row for row in self.repo.load().items if row.stable_key == item.stable_key)
        self.assertEqual(saved.cover_status, 'downloaded')
        self.assertFalse(saved.cover_url)






if __name__=='__main__': unittest.main()
