"""No user models/data: remove avoidable work from navigation."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
import tempfile
import hashlib
from src.ui.main_window import MainWindow

class NavigationCostsTests(unittest.TestCase):
    def test_web_workspace_keeps_frame_without_synthetic_resize(self):
        from PySide6.QtCore import QSize
        calls=[]
        page=SimpleNamespace(resize=lambda *args:calls.append(args),layout=lambda:None)
        shell=SimpleNamespace(library_page=page,stack=SimpleNamespace(currentWidget=lambda:page,size=lambda:QSize(1200,800)))
        MainWindow._relayout_active_page(shell)
        self.assertEqual(calls,[(QSize(1200,800),)])

    def test_voice_page_never_scans_hidden_model_size(self):
        from tests.test_voice_page_model_shell import VoicePageModelShellTests,VoicePage,_Cfg,_Main
        f=VoicePageModelShellTests();f.setUp()
        try:
            with patch('src.ui.voice_page.local_models.is_downloaded',return_value=True), patch('src.ui.voice_page.local_models.on_disk_size',side_effect=AssertionError('navigation scanned model tree')),patch('src.ui.qwen3_workbench.qwen3_runtime.readiness',return_value=(False,'隔离未安装')):
                page=VoicePage(_Cfg(f.root),_Main())
                try:page.refresh()
                finally:page._qwen3.shutdown();page.close()
        finally:f.tearDown()

    def test_collection_media_binds_visible_identity_without_reloading_repository(self):
        from src.ui.collection_page import CollectionPage
        from src.core.collect.media import ManagedMediaStore
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()/'media';(root/'douyin').mkdir(parents=True)
            key='douyin:123456'
            relative='douyin/'+hashlib.sha256(key.encode()).hexdigest()[:24]+'-'+hashlib.sha256(b'test media').hexdigest()[:20]+'.mp4'
            (root/relative).write_bytes(b'test media')
            item=SimpleNamespace(stable_key=key,media_status='downloaded',media_local_path=relative)
            page=SimpleNamespace(_data_location_path=lambda _:root)
            with patch.object(ManagedMediaStore,'_repository_owner',side_effect=AssertionError('repeated repository read')):
                self.assertEqual(CollectionPage._managed_media_file(page,item),root/relative)
                with patch.object(ManagedMediaStore,'_entry_info',side_effect=AssertionError('display read video bytes')):
                    self.assertEqual(CollectionPage._existing_media_file(page,item),root/relative)
                (root/relative).write_bytes(b'corrupted content')
                self.assertIsNone(CollectionPage._managed_media_file(page,item))
                (root/relative).unlink()
                (root/relative).symlink_to(root/'missing')
                self.assertIsNone(CollectionPage._existing_media_file(page,item))
                item.stable_key='douyin:654321'
                self.assertIsNone(CollectionPage._managed_media_file(page,item))
