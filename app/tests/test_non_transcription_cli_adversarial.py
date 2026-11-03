"""Non-transcription CLI failure injection; temporary data, no keys or live network."""
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QWidget

from src.config import AppConfig
from src.core import library
from src.core.agent_workspace.control import SETTINGS
from src.core.agent_workspace.documents import digest, snapshot, save
from src.core.agent_workspace.store import encode
from src.core.collect.models import CollectedItem
from src.core.models import Transcript
from src.core.updater.models import UpdateSnapshot
from src.ui.cli_control import CliControl
from tests import test_agent_workspace as workspace_fixtures
from tests import test_cli_expansion as expansion_fixtures


class NonTranscriptionWindowReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window.cfg = AppConfig()
        self.window.cfg.save = Mock(return_value=True)
        self.window._on_models_changed = Mock()
        self.window.update_controller = SimpleNamespace(snapshot=UpdateSnapshot(), _worker=None)
        self.control = CliControl(self.window, enabled=False)
        self.addCleanup(self.window.deleteLater)

    def command(self, domain, action, arguments=None):
        return dict(request_id='review-request', domain=domain, action=action,
                    arguments=arguments or {}, session=self.control.nonce)

    def test_settings_save_exception_restores_all_window_values(self):
        previous = {key: getattr(self.window.cfg, key) for key in SETTINGS}
        self.window.cfg.save.side_effect = OSError('isolated disk full')
        with self.assertRaises((OSError, ValueError)):
            self.control.execute(self.command('settings', 'apply', dict(
                changes={'prefer_subtitles': not previous['prefer_subtitles'], 'keep_media': not previous['keep_media']},
                expected_hash=digest(encode(previous)))))
        self.assertEqual({key: getattr(self.window.cfg, key) for key in SETTINGS}, previous)
        self.window._on_models_changed.assert_not_called()

    def test_update_signature_failure_without_error_code_is_never_success(self):
        self.window.update_controller.snapshot = UpdateSnapshot(
            state='failed_signature', ui_state='manifest_verify_error', error_code='')
        self.control.active['review-request'] = dict(domain='updates', action='check')
        self.control._track_updates()
        self.assertEqual(self.control.reports[-1]['state'], 'failed')

    def test_update_download_pause_reports_paused_not_completed_or_failed(self):
        self.window.update_controller.snapshot = UpdateSnapshot(
            state='download_paused', ui_state='download_paused', downloaded=4, total=10,
            error_code='cancelled', message='下载已暂停')
        self.control.active['review-request'] = dict(domain='updates', action='download')
        self.control._track_updates()
        self.assertEqual(self.control.reports[-1]['state'], 'paused')
        self.assertEqual(self.control.reports[-1]['result']['downloaded'], 4)

    def test_update_handover_refuses_another_check_before_dispatch(self):
        controller = self.window.update_controller
        controller.snapshot = UpdateSnapshot(state='installing', ui_state='preparing')
        controller.check = Mock()
        with self.assertRaisesRegex(ValueError, '更新|接管'):
            self.control.execute(self.command('updates', 'check'))
        controller.check.assert_not_called()

    def test_update_worker_loss_requires_inspection_instead_of_completion(self):
        self.window.update_controller.snapshot = UpdateSnapshot(state='checking', ui_state='checking_manual')
        self.control.active['review-request'] = dict(domain='updates', action='check')
        self.control._track_updates()
        self.assertEqual(self.control.reports[-1]['state'], 'needs_input')


class NonTranscriptionWorkspaceReview(unittest.TestCase):
    def setUp(self):
        workspace_fixtures.AgentWorkspaceTests.setUp(self)
        self.config = SimpleNamespace(voice_library_dir=str(self.root/'voices'), obsidian_dir='')
        load = patch('src.config.AppConfig.load', return_value=self.config)
        load.start()
        self.addCleanup(load.stop)
    runtime = expansion_fixtures.ExpansionTests.runtime
    invoke = expansion_fixtures.ExpansionTests.invoke
    call = expansion_fixtures.ExpansionTests.call

    def test_folder_order_rejects_duplicate_and_missing_ids_without_changes(self):
        first = self.call('folders.create', dict(name='先整理'))['id']
        second = self.call('folders.create', dict(name='后整理'))['id']
        self.call('folders.order', dict(ordered_ids=[second, first]))
        previous = library.FOLDERS_FILE.read_bytes()
        for ordered in ([first, first], [first, 'missing-folder']):
            code, _ = self.invoke('folders.order', dict(ordered_ids=ordered))
            self.assertNotEqual(code, 0)
            self.assertEqual(library.FOLDERS_FILE.read_bytes(), previous)
        self.assertEqual([f['id'] for f in library.list_folders('transcript')], [second, first])

    def test_obsidian_edited_version_gets_new_note_and_replay_reuses_it(self):
        args = dict(document_id=self.eid, request_id='obs-original', folder=str(self.root))
        original = self.call('documents.obsidian', args)
        original_bytes = Path(original['path']).read_bytes()
        current = snapshot(self.eid)
        save(self.eid, '修订后的完整正文🙂。', current['title'], current['revision'],
             current['content_hash'], 'edit-before-obsidian')
        changed = self.call('documents.obsidian', dict(args, request_id='obs-changed'))
        self.assertNotEqual(changed['path'], original['path'])
        self.assertEqual(Path(original['path']).read_bytes(), original_bytes)
        self.assertIn('修订后的完整正文🙂。', Path(changed['path']).read_text())
        replay = self.call('documents.obsidian', dict(args, request_id='obs-changed-again'))
        self.assertEqual(replay['path'], changed['path'])
        self.assertTrue(replay['reused'])

    def test_legacy_obsidian_receipt_reuses_existing_note_across_export_dates(self):
        from datetime import date
        from src.core.agent_workspace.operations import publish_bytes
        from src.core.agent_workspace.store import WorkspaceStore
        from src.core.obsidian import build_note, _safe_name
        class PriorDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 10, 30)
        doc = snapshot(self.eid)
        legacy = 'obsidian-' + digest(encode([self.eid, str(self.root), doc['content_hash']]))
        target = self.root/(_safe_name(doc['title'])+'-'+digest(self.eid)[:8]+'.md')
        with patch('src.core.obsidian.date', PriorDate):
            content = build_note(library.load(self.eid), doc['title'], text_override=doc['text'])
        runtime = self.runtime()
        publish_bytes(WorkspaceStore(), legacy, str(target), content.encode(),
                      [self.eid, doc['revision'], doc['content_hash'], 'obsidian'])
        result = self.call('documents.obsidian', dict(document_id=self.eid, folder=str(self.root),
                           request_id='legacy-replay'), runtime)
        self.assertEqual(result['path'], str(target))
        self.assertTrue(result['reused'])
        self.assertEqual(target.read_text(), content)
        self.assertEqual(len(list(self.root.glob('*.md'))), 1)

    def test_export_directory_replacement_is_not_reported_as_success(self):
        from src.core import link_extraction_jobs as exports
        target = self.root/'output'
        target.mkdir()
        moved = self.root/'moved-output'
        real_check = exports._check_destination
        swapped = False
        def swap_after_check(parent, identity):
            nonlocal swapped
            real_check(parent, identity)
            if not swapped:
                swapped = True
                target.rename(moved)
                target.mkdir()
                (target/'user-file').write_text('keep user file')
        with patch.object(exports, '_check_destination', side_effect=swap_after_check):
            code, _ = self.invoke('documents.export', dict(document_id=self.eid,
                format='md', path=str(target/'result.md'), request_id='changed-directory'))
        self.assertNotEqual(code, 0)
        self.assertEqual((target/'user-file').read_text(), 'keep user file')
        self.assertFalse((target/'result.md').exists())
        self.assertIn('第一段', library.load(self.eid).text)

    def test_excel_overlong_cell_cannot_silently_truncate_saved_content(self):
        from src.core.collect.export import write_xlsx, write_csv
        item = CollectedItem('12345', title='长文稿', work_url='https://www.douyin.com/video/12345')
        text = '中文🙂' * 14000 + '末尾必须完整'
        target = self.root/'long.xlsx'
        with self.assertRaisesRegex(ValueError, '32767|32,767|CSV'):
            write_xlsx([item], target, transcripts={item.stable_key:text})
        self.assertFalse(target.exists())
        csv_path = write_csv([item], self.root/'long.csv', transcripts={item.stable_key:text})
        with csv_path.open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual(next(csv.DictReader(stream))['转录文稿'], text)

    def test_public_spreadsheet_text_remains_text_in_csv_and_excel(self):
        from src.core.collect.export import write_csv, write_xlsx
        from openpyxl import load_workbook
        item = CollectedItem('12345', title='=1+1', author_name='@SUM(1)',
                             work_url='https://www.douyin.com/video/12345')
        xlsx = write_xlsx([item], self.root/'text.xlsx')
        book = load_workbook(xlsx, data_only=False)
        self.addCleanup(book.close)
        self.assertEqual(book.active['B2'].value, '=1+1')
        self.assertEqual(book.active['B2'].data_type, 's')
        path = write_csv([item], self.root/'text.csv')
        with path.open(encoding='utf-8-sig', newline='') as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row['标题'], "'=1+1")
        self.assertEqual(row['作者昵称'], "'@SUM(1)")

    def test_gui_and_cli_voice_imports_preserve_both_index_entries(self):
        from src.core.synthesis import voices
        source = self.root/'reference.wav'
        with wave.open(str(source), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 1600)
        entered, release, cli_done = threading.Event(), threading.Event(), threading.Event()
        original_read = voices._read
        errors = []
        def blocked_gui_read():
            value = original_read()
            if threading.current_thread().name == 'isolated-gui':
                entered.set()
                if not release.wait(4):
                    raise AssertionError('isolated fixture timed out')
            return value
        def gui():
            try:
                voices.add_voice('GUI音色', str(source))
            except Exception as exc:
                errors.append(exc)
        def cli():
            try:
                self.call('voices.create', dict(request_id='concurrent-voice', name='CLI音色', source_path=str(source)))
            except Exception as exc:
                errors.append(exc)
            finally:
                cli_done.set()
        info = dict(audio=True, sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        with patch.object(voices, 'VOICES_DIR', self.root/'voices'), \
                patch.object(voices, 'INDEX', self.root/'voices/voices.json'), \
                patch.object(voices, '_read', side_effect=blocked_gui_read), \
                patch('src.core.live_recording.media.probe', return_value=info):
            first = threading.Thread(target=gui, name='isolated-gui')
            second = threading.Thread(target=cli, name='isolated-cli')
            try:
                first.start()
                self.assertTrue(entered.wait(2))
                second.start()
                cli_done.wait(.5)
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual({v.name for v in voices.list_voices()}, {'GUI音色', 'CLI音色'})
        self.assertTrue(source.exists())

    def test_cli_voice_library_follows_saved_location_after_it_changes(self):
        from src.core.synthesis import voices
        selected = self.root/'selected-voices'
        selected.mkdir()
        original = self.root/'stale-voices'
        original.mkdir()
        (selected/'voices.json').write_text(json.dumps([dict(id='a'*12, name='选中库',
            reference_audio_path=str(selected/'reference.wav'), reference_text='', engine='local_voxcpm2',
            created_at='2026-10-31T12:00:00', meta={})]))
        self.config.voice_library_dir = str(selected)
        with patch.object(voices, 'VOICES_DIR', original), patch.object(voices, 'INDEX', original/'voices.json'):
            result = self.call('voices.list', {})
            self.assertEqual([v['name'] for v in result['voices']], ['选中库'])
            self.config.voice_library_dir = str(original)
            self.assertEqual(self.call('voices.list', {})['voices'], [])
        self.assertTrue((selected/'voices.json').exists())

    def test_other_process_voice_write_returns_busy_then_safe_retry_preserves_both(self):
        from src.core.synthesis import voices
        source = self.root/'process-reference.wav'
        with wave.open(str(source), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 1600)
        program = r'''
import json,sys,time
from pathlib import Path
from src.core.synthesis import voices
root,source,signals=map(Path,sys.argv[1:])
voices.configure_voice_library_dir(root)
read=voices._read
def hold():
    result=read()
    (signals/'entered').touch()
    deadline=time.monotonic()+5
    while not (signals/'release').exists():
        if time.monotonic()>deadline:raise RuntimeError('isolated fixture expired')
        time.sleep(.01)
    return result
voices._read=hold
voice=voices.add_voice('独立进程音色',str(source))
print(json.dumps({'id':voice.id}))
'''
        signals = self.root/'signals'
        signals.mkdir()
        root = self.root/'voices'
        child = subprocess.Popen([sys.executable, '-B', '-c', program, str(root), str(source), str(signals)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        done = threading.Event()
        responses = []
        def request():
            try:
                responses.append(self.invoke('voices.create', dict(request_id='process-voice',
                                 name='CLI并发音色', source_path=str(source))))
            finally:
                done.set()
        info = dict(audio=True, sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        client = threading.Thread(target=request)
        with patch.object(voices, 'VOICES_DIR', root), patch.object(voices, 'INDEX', root/'voices.json'), \
                patch('src.core.live_recording.media.probe', return_value=info):
            try:
                deadline = time.monotonic()+3
                while not (signals/'entered').exists() and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertTrue((signals/'entered').exists())
                client.start()
                responsive = done.wait(.7)
            finally:
                (signals/'release').touch()
                output, error = child.communicate(timeout=5)
                if client.ident is not None:
                    client.join(3)
            self.assertEqual(child.returncode, 0, error)
            self.assertFalse(client.is_alive())
            self.assertTrue(responsive, 'A different process must not freeze the caller while holding the voice index')
            self.assertEqual(len(responses), 1)
            self.assertNotEqual(responses[0][0], 0)
            self.assertEqual(responses[0][1]['error']['code'], 'RESOURCE_BUSY')
            self.call('voices.create', dict(request_id='process-voice', name='CLI并发音色', source_path=str(source)))
            self.assertEqual({v.name for v in voices.list_voices()}, {'独立进程音色', 'CLI并发音色'})
            self.assertIn(json.loads(output)['id'], {v.id for v in voices.list_voices()})


if __name__ == '__main__':
    unittest.main()
