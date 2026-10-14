"""Library recovery without modifying original documents or damaged records."""
import json
import unittest
from unittest.mock import patch

from tests import test_agent_workspace as fixtures
from src.config import AppConfig
from src.core import library
from src.core.agent_workspace.service import WorkspaceService
from src.core.agent_workspace.store import workspace_root


class DocumentVisibilityTests(unittest.TestCase):
    setUp = fixtures.AgentWorkspaceTests.setUp

    def bootstrap(self):
        with patch.object(AppConfig, 'load', return_value=AppConfig()):
            return self.service.bootstrap()

    def test_recovery_discovery_keeps_unchanged_library_reads_cached(self):
        self.service.list()
        with patch.object(library, '_load_raw', wraps=library._load_raw) as reads:
            self.assertEqual(self.service.list()['entries'][0]['id'], self.eid)
        reads.assert_not_called()

    def test_bad_selected_body_does_not_hide_healthy_documents(self):
        damaged = library.save_document('正文副本仍完整', '异常稿')
        self.service.view_state('selected_document', damaged)
        md = library.markdown_path(damaged)
        md.write_bytes(b'\xff\xfeinvalid')
        original = (library.LIBRARY_DIR / f'{damaged}.json').read_bytes()
        result = self.bootstrap()
        self.assertEqual(result['screen']['listing']['total'], 2)
        opened = result['screen']['content']
        self.assertEqual(opened['document']['text'], '正文副本仍完整')
        self.assertTrue(opened['document']['read_only'])
        self.assertEqual(md.read_bytes(), b'\xff\xfeinvalid')
        self.assertEqual((library.LIBRARY_DIR / f'{damaged}.json').read_bytes(), original)

    def test_conversation_corruption_preserves_document_reading_and_original_bytes(self):
        self.bootstrap()
        db = workspace_root() / 'conversations.sqlite3'
        db.write_bytes(b'broken')
        index = library.LIBRARY_DIR / f'{self.eid}.json'
        original = index.read_bytes()
        result = self.bootstrap()
        self.assertEqual(result['screen']['listing']['total'], 1)
        opened = WorkspaceService().open(self.eid, include_workspace=True)
        self.assertTrue(opened['document']['text'])
        self.assertIsNone(opened['conversation'])
        self.assertTrue(opened['document']['read_only'])
        self.assertEqual(db.read_bytes(), b'broken')
        self.assertEqual(index.read_bytes(), original)

    def test_corrupt_migration_receipt_does_not_hide_documents_or_get_overwritten(self):
        self.bootstrap()
        marker = workspace_root() / 'migration-v1.json'
        marker.write_text('{broken')
        self.assertEqual(self.bootstrap()['screen']['listing']['total'], 1)
        self.assertEqual(marker.read_text(), '{broken')

    def test_orphan_body_is_visible_and_can_be_recovered_without_overwriting_original(self):
        damaged = library.save_document('找回这份正文', '旧标题')
        index = library.LIBRARY_DIR / f'{damaged}.json'
        md = library.markdown_path(damaged)
        for state in ('missing', 'corrupt'):
            with self.subTest(state=state):
                index.unlink(missing_ok=True)
                if state == 'corrupt': index.write_text('{broken')
                rows = self.service.list()['entries']
                self.assertIn(damaged, [r['id'] for r in rows])
                opened = self.service.open(damaged, include_workspace=True)['document']
                self.assertEqual(opened['text'], '找回这份正文')
                self.assertTrue(opened['recovery_required'])
                copied = self.service.recover_document(damaged, opened['content_hash'], 'recover-' + state)
                self.assertNotEqual(copied['document_id'], damaged)
                self.assertEqual(library.load_markdown(copied['document_id']), '找回这份正文')
                self.assertEqual(md.read_text(), '找回这份正文')
                if state == 'corrupt': self.assertEqual(index.read_text(), '{broken')
                self.assertEqual(self.service.recover_document(damaged, opened['content_hash'], 'recover-' + state), copied)

    def test_recovery_refuses_changed_body_and_symlink(self):
        index = library.LIBRARY_DIR / f'{self.eid}.json'
        md = library.markdown_path(self.eid)
        index.unlink()
        doc = self.service.open(self.eid)['document']
        md.write_text('外部的新修改')
        with self.assertRaises(library.RevisionConflict):
            self.service.recover_document(self.eid, doc['content_hash'], 'changed')
        held = md.with_suffix('.held')
        md.rename(held)
        md.symlink_to(held)
        self.assertNotIn(self.eid, [r['id'] for r in self.service.list()['entries']])
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.service.open(self.eid)
        self.assertEqual(held.read_text(), '外部的新修改')


if __name__ == '__main__': unittest.main()
