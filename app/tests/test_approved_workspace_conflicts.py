"""Regressions for conflicts between the approved views and their local owners."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject
from PySide6.QtWidgets import QApplication

from src.config import AppConfig
from src.core.collect.history import CollectionHistoryRepository
from src.core.collect.models import CollectedItem
from src.core.user_notifications import AuthorizationStatus, PermissionResult
from src.ui.approved_collection import ApprovedCollection
from src.ui.approved_settings import ApprovedSettings
from src.ui.approved_workspaces import WorkspaceBridge
from src.ui.extraction.widget import ExtractionWorkspace
from src.ui.main_window import MainWindow


class ApprovedWorkspaceConflictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.addCleanup(self.app.processEvents)
        self.addCleanup(QCoreApplication.sendPostedEvents, None, QEvent.DeferredDelete)

    def test_large_selection_keeps_explicit_bounds_by_operation(self):
        owner = QObject()
        bridge = WorkspaceBridge(owner, lambda: None, {
            'export': lambda done, *, keys: done(len(keys)),
            'ordinary': lambda done, *, keys: done(len(keys)),
        }, {'export': 2_000_000})
        replies = []
        bridge.replied.connect(lambda token, reply: replies.append((token, json.loads(reply))))
        keys = [f'douyin:{7000000000000000000+i}' for i in range(3000)]
        payload = json.dumps({'keys': keys})
        bridge.ready()
        bridge.request('1', 'export', payload)
        bridge.request('2', 'ordinary', payload)
        bridge.request('3', 'export', ' ' * 2_000_001)
        self.assertEqual(replies[0][1]['value'], 3000)
        self.assertFalse(replies[1][1]['ok'])
        self.assertFalse(replies[2][1]['ok'])

    def test_read_only_record_tracks_displayed_works_without_writing_history(self):
        with TemporaryDirectory() as directory:
            history = Path(directory) / 'records.json'
            repo = CollectionHistoryRepository(history)
            ids = []
            for number in ('1', '2'):
                ids.append(repo.add_record(
                    [CollectedItem(number, work_id=number)], platform='douyin',
                    homepage_url='https://www.douyin.com/user/fixture',
                    source_method='anonymous_latest', completion='complete',
                ).selected_record_id)
            data = json.loads(history.read_text())
            data['active_records'].append({'record_id': 'broken'})
            history.write_text(json.dumps(data))
            before = history.read_bytes()
            cfg = AppConfig(auto_update_enabled=False)
            with patch('src.ui.collection_page.is_logged_in', return_value=False):
                page = ApprovedCollection(cfg, history_repository=repo)
            self.addCleanup(page.deleteLater)
            self.addCleanup(page.shutdown_workers)
            self.assertFalse(page._history_snapshot.writable)
            answers = []
            page._select_record(answers.append, id=ids[0], version=page._history_snapshot.source_hash)
            self.assertEqual(answers[0]['selected'], ids[0])
            self.assertEqual(answers[0]['works'][0]['id'], 'douyin:1')
            self.assertEqual(page._items_for(ids[0], ['douyin:1'])[0].stable_key, 'douyin:1')
            with self.assertRaises(ValueError):
                page._items_for(ids[1], ['douyin:1'])
            self.assertEqual(history.read_bytes(), before)
            self.assertEqual(page.table.rowCount(), 0)

    def _settings(self, notifications=None):
        cfg = AppConfig(auto_update_enabled=False, notification_policy='never')
        page = ApprovedSettings(cfg, notification_service=notifications,
                                agent_skill_status_provider=lambda: 'not_installed', lazy_categories=True)
        self.addCleanup(page.deleteLater)
        self.addCleanup(page.shutdown_workers)
        return cfg, page

    def test_late_permission_cannot_override_latest_notification_choice(self):
        class Notifications:
            def __init__(self):
                self.requests = []

            def request_enable(self, callback):
                self.requests.append(callback)

        notifications = Notifications()
        cfg, page = self._settings(notifications)
        with patch.object(cfg, 'save', return_value=True) as save:
            for choice in ('always', 'never'):
                page._preference(lambda *args, **kwargs: None, key='notification', value=choice)
            notifications.requests[0](PermissionResult(True, AuthorizationStatus.AUTHORIZED, 'fixture'))
            self.assertEqual(cfg.notification_policy, 'never')
            save.assert_not_called()
            for choice in ('always', 'inactive'):
                page._preference(lambda *args, **kwargs: None, key='notification', value=choice)
            self.assertEqual(len(notifications.requests), 2)
            notifications.requests[1](PermissionResult(True, AuthorizationStatus.AUTHORIZED, 'fixture'))
            self.assertEqual(cfg.notification_policy, 'inactive')
            save.assert_called_once()

    def test_close_flushes_latest_font_and_can_report_failed_save(self):
        cfg, page = self._settings()
        previews = []
        page.font_preview.connect(previews.append)
        with patch.object(cfg, 'save', return_value=True) as save:
            for percent in (114, 129):
                page._font_preview(lambda _: None, percent=percent)
            self.assertEqual(cfg.interface_font_percent, 100)
            page.shutdown_workers()
            self.assertEqual(cfg.interface_font_percent, 129)
            save.assert_called_once()
        cfg, page = self._settings()
        page.font_preview.connect(previews.append)
        with patch.object(cfg, 'save', return_value=False):
            page._font_preview(lambda _: None, percent=99)
            self.assertFalse(page.flush_font())
            self.assertEqual(cfg.interface_font_percent, 100)
            self.assertEqual(previews[-2:], [99, 100])

    def test_explicit_mode_rolls_back_on_failure_and_persists_on_success(self):
        cfg = AppConfig(auto_update_enabled=False, collection_mode='partial')
        with patch('src.ui.collection_page.is_logged_in', return_value=False):
            with TemporaryDirectory() as directory:
                page = ApprovedCollection(cfg, history_repository=CollectionHistoryRepository(Path(directory) / 'records.json'))
        self.addCleanup(page.deleteLater)
        self.addCleanup(page.shutdown_workers)
        replies = []

        def reply(value=None, *, message=''):
            replies.append((value, message))

        with patch.object(cfg, 'save', return_value=False):
            page._mode(reply, value='full')
        self.assertEqual((cfg.collection_mode, page._selected_mode), ('partial', 'partial'))
        self.assertIn('未保存', replies[-1][1])
        with patch.object(cfg, 'save', return_value=True):
            page._mode(reply, value='full')
        self.assertEqual((cfg.collection_mode, page._selected_mode), ('full', 'full'))
        self.assertEqual(replies[-1], (True, ''))

    def test_extraction_fallback_does_not_inherit_collection_download_options(self):
        cfg = AppConfig(keep_media=True, collect_cover=True, link_media_mode='video')
        workspace = SimpleNamespace(main=SimpleNamespace(cfg=cfg))
        policy = ExtractionWorkspace.initial_policy(workspace, 'https://example.invalid/work', 'link')
        self.assertEqual(policy.requested_outputs, ('text',))
        self.assertFalse(policy.material_snapshot.save_media)
        self.assertFalse(policy.material_snapshot.save_cover)

    def test_approved_collection_does_not_stage_legacy_tasks_without_save_confirmation(self):
        window = SimpleNamespace(extraction_workspace=SimpleNamespace(batches=SimpleNamespace(enabled=False)))
        receipt = MainWindow._collect_transcribe_group(window, [], wait_for_save=True)
        with self.assertRaisesRegex(RuntimeError, '任务服务暂不可用'):
            receipt.result()
