import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from src.config import AppConfig
from src.core import library
from src.core.agent_workspace.store import workspace_root
from src.core.updater import helper_entry, install, process_identity, restart_recovery
from src.core.agent_workspace import documents
from src.core.updater.install import InstallTransaction
from src.core.updater.archive import app_file_manifest
from src.core.updater.state import StateStore
from src.core.updater.models import UpdateError
from tests import test_auto_update as update_fixture
from tests import test_agent_workspace as document_fixture

class DocumentAdversarialTests(unittest.TestCase):
    setUp = document_fixture.AgentWorkspaceTests.setUp
    def test_structurally_corrupt_migration_never_hides_library(self):
        with patch.object(AppConfig, 'load', return_value=AppConfig()):
            self.service.bootstrap()
            marker = workspace_root() / 'migration-v1.json'
            valid = json.loads(marker.read_text())
            for value in ([], {}, {'phase':'complete', 'errors':42}, dict(valid, phase=[]), dict(valid, phase={})):
                with self.subTest(value=value):
                    body = json.dumps(value)
                    marker.write_text(body)
                    result = self.service.bootstrap()
                    self.assertEqual(result['migration']['phase'], 'unavailable')
                    self.assertEqual(result['screen']['listing']['total'], 1)
                    self.assertEqual(marker.read_text(), body)
    def test_structurally_corrupt_pending_save_preserves_readonly_document(self):
        with patch.object(AppConfig, 'load', return_value=AppConfig()):
            self.service.bootstrap()
            self.service.view_state('selected_document', self.eid)
            marker = workspace_root() / 'pending' / (self.eid + '.json')
            marker.parent.mkdir(exist_ok=True)
            original = library.markdown_path(self.eid).read_bytes()
            for value in ({}, [], {'request_id':'saved', 'receipt':[]}, {'request_id':'saved', 'receipt':{'document_id':self.eid}}):
                with self.subTest(value=value):
                    body = json.dumps(value)
                    marker.write_text(body)
                    result = self.service.bootstrap()
                    self.assertEqual(result['screen']['listing']['total'], 1)
                    self.assertTrue(result['screen']['content']['document']['read_only'])
                    self.assertEqual(marker.read_text(), body)
                    self.assertEqual(library.markdown_path(self.eid).read_bytes(), original)

    def test_corrupt_receipt_state_preserves_body_and_marker(self):
        with patch.object(AppConfig, 'load', return_value=AppConfig()):
            self.service.bootstrap(); self.service.view_state('selected_document', self.eid)
            before = documents.snapshot(self.eid)
            documents.save(self.eid, before['text'], before['title'], before['revision'], before['content_hash'], 'saved')
            operation = workspace_root()/'operations/saved.json'
            receipt = json.loads(operation.read_text()); marker = workspace_root()/'pending'/(self.eid+'.json')
            for state in ([], {}, None):
                with self.subTest(state=state):
                    corrupted = dict(receipt, state=state)
                    operation.write_text(json.dumps(corrupted))
                    marker.write_text(json.dumps({'request_id':'saved', 'receipt':corrupted}))
                    result = self.service.bootstrap()
                    self.assertEqual(result['screen']['listing']['total'], 1)
                    self.assertTrue(result['screen']['content']['document']['read_only'])
                    self.assertEqual(library.markdown_path(self.eid).read_text(), before['text'])
                    self.assertEqual(json.loads(operation.read_text()), corrupted)
                    self.assertTrue(marker.exists())

    def test_corrupt_folder_kind_never_hides_healthy_documents(self):
        with patch.object(AppConfig, 'load', return_value=AppConfig()):
            self.service.bootstrap()
            for kind in ([], {}, None, 42):
                with self.subTest(kind=kind):
                    body = json.dumps({'folders':[{'id':'folder-fixture','name':'Fixture','kind':kind}]})
                    library.FOLDERS_FILE.write_text(body)
                    result = self.service.bootstrap()
                    self.assertEqual(result['screen']['listing']['total'], 1)
                    self.assertTrue(result['screen']['listing']['errors'])
                    self.assertEqual(library.FOLDERS_FILE.read_text(), body)


class LiveRecoveryAdversarialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.services = []

    def tearDown(self):
        for service in reversed(self.services):
            service.shutdown()

    def close_service(self, service):
        service.shutdown()
        self.services.remove(service)

    def fixture(self, root, paths):
        from src.core.live_recording.index import LiveIndex
        index = LiveIndex(root/'live-recording')
        for number, path in enumerate(paths):
            index.write('INSERT INTO recordings VALUES(?,?,?,?,?,?,?,?,?)',
                ('recording-'+str(number)*32, 'host-'+'b'*32, 'Fixture', 'job-fixture',
                 str(path), 1.0, None, '', '[]'))
        index.close()

    def open_service(self, root):
        from src.core.live_recording.service import LiveService
        service = LiveService(root, SimpleNamespace(list=lambda:[]), 'fixture',
            ffmpeg='/fixture/ffmpeg', secret_read=lambda:'', provider=object())
        self.services.append(service)
        return service

    def wait_recovery(self, service):
        deadline = time.monotonic()+3
        while any(w['state']=='running' for w in service.exports.values()) and time.monotonic()<deadline:
            time.sleep(.01)
        work = next(iter(service.exports.values()))
        self.assertNotEqual(work['state'], 'running')
        return work

    def test_missing_disk_retries_on_reopen_and_does_not_block_other_recordings(self):
        root = self.root
        missing, healthy = root/'disconnected-media', root/'connected-media'
        healthy.mkdir(); (healthy/'part-000000.ts').write_bytes(b'healthy sample')
        self.fixture(root, [missing, healthy])
        service = self.open_service(root)
        with patch('src.core.live_recording.service.probe', return_value={'duration':2,'bytes':14,'sha256':'a'*64}):
            service.handle('open', {'nonce':'c'*32})
            self.assertEqual(self.wait_recovery(service)['state'], 'failed')
        self.assertEqual(len(service.index.all('SELECT * FROM segments WHERE valid=1')), 1)
        self.assertEqual(service.index.one('SELECT reason FROM recordings WHERE directory=?', (str(missing),))['reason'], 'recovery_required')
        self.close_service(service)
        missing.mkdir(); media = missing/'part-000000.ts'; media.write_bytes(b'preserved sample')
        reopened = self.open_service(root)
        self.assertEqual(len(reopened.recovery), 1)
        with patch('src.core.live_recording.service.probe', return_value={'duration':3,'bytes':16,'sha256':'b'*64}):
            reopened.handle('open', {'nonce':'d'*32})
            self.assertEqual(self.wait_recovery(reopened)['state'], 'succeeded')
        self.assertEqual(len(reopened.index.all('SELECT * FROM segments WHERE valid=1')), 2)
        self.assertEqual(media.read_bytes(), b'preserved sample')
        self.close_service(reopened)
        self.assertEqual(self.open_service(root).recovery, [])

    def test_cancelled_scan_remains_pending_for_next_start(self):
        root = self.root; media = root/'media'; media.mkdir()
        (media/'part-000000.ts').write_bytes(b'preserved sample')
        self.fixture(root, [media]); service = self.open_service(root)
        entered, resume = threading.Event(), threading.Event()
        def paused_probe(*args, **kwargs):
            entered.set(); resume.wait(2)
            return {'duration':3,'bytes':16,'sha256':'b'*64}
        with patch('src.core.live_recording.service.probe', side_effect=paused_probe):
            token = service.handle('open', {'nonce':'c'*32})['session']
            self.assertTrue(entered.wait(2))
            service.handle('stop', {'session':token}); resume.set()
            self.assertEqual(self.wait_recovery(service)['state'], 'cancelled')
        self.close_service(service)
        self.assertEqual(len(self.open_service(root).recovery), 1)

class UpdateAdversarialTests(unittest.TestCase):
    def transaction(self, root):
        _, target, staged = update_fixture.AutoUpdateTests._apps(self, root)
        tx = InstallTransaction(root/'updates', allowed_targets={target}, verify_system=False)
        path, plan = tx.create_plan(target=target, staged_app=staged, version='0.2.4', build='20260712.1', old_pid=1,
            old_pid_start_time='fixture', old_executable_path='/fixture', staged_sha=app_file_manifest(staged)[1])
        return tx, target, path, plan
    def test_failed_process_query_never_authorizes_app_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            tx, target, path, _ = self.transaction(Path(directory))
            with patch.object(process_identity.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ps',3)):
                with self.assertRaises(UpdateError):
                    tx.execute_local(path, launch=lambda *_:None, wait_ready=lambda *_:False,
                        old_pid_validator=lambda pid,*_:process_identity.process_identity(pid) is None, crash_at='after_exchange')
            self.assertTrue((target/'old').exists())
            self.assertTrue(tx.lock.exists())
    def test_recovery_never_removes_a_replaced_artifact_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            tx, target, path, plan = self.transaction(Path(directory))
            tx.execute_local(path, launch=lambda *_:None, wait_ready=lambda *_:False,
                old_pid_validator=lambda *_:True, crash_at='after_copy')
            swap = Path(plan['swap_new']).parent
            kept = swap.with_name(swap.name+'.kept')
            swap.rename(kept); swap.mkdir(mode=0o700)
            unrelated = swap/'user-document.txt'; unrelated.write_text('keep')
            tx.recover(path)
            self.assertEqual(unrelated.read_text(),'keep')
            self.assertTrue(kept.exists())
            self.assertTrue((target/'old').exists())
    def test_recovery_cleanup_failure_keeps_installed_result(self):
        with tempfile.TemporaryDirectory() as directory:
            tx, target, path, _ = self.transaction(Path(directory))
            tx.execute_local(path, launch=lambda *_:None, wait_ready=lambda *_:True,
                old_pid_validator=lambda *_:True, crash_at='after_confirmation')
            with patch.object(install,'_remove_tree_at',side_effect=PermissionError('fixture')):
                self.assertEqual(tx.recover(path),'installed')
            self.assertEqual(StateStore(tx.root).load().ui_state,'installed')
            self.assertTrue((target/'new').exists())
            self.assertFalse(tx.lock.exists())

    @patch('src.core.updater.helper_entry.PreSwapHandover')
    def test_completion_write_failure_stops_new_child_before_rollback(self, _handover):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); tx, target, path, _ = self.transaction(root)
            (path.parent/'result.json').symlink_to(root/'missing-receipt')
            class Child:
                pid = 987654
                stopped = False
                def poll(self): return 0 if self.stopped else None
                def terminate(self): self.stopped = True
                def wait(self, timeout=None): self.stopped = True; return 0
            child = Child(); original_popen = helper_entry.subprocess.Popen
            def popen(command, **kwargs):
                return child if command[0] == str(target/'Contents/MacOS/Lulu') else original_popen(command, **kwargs)
            def identity(pid):
                return ('new-start', str(target/'Contents/MacOS/Lulu')) if pid==child.pid else None
            recover = tx.recover
            def checked_recover(plan_path):
                self.assertTrue(child.stopped, 'The child must exit before files can be exchanged')
                return recover(plan_path)
            with patch.object(helper_entry, 'InstallTransaction', return_value=tx), \
                    patch.object(helper_entry, 'process_identity', side_effect=identity), \
                    patch.object(helper_entry.subprocess, 'Popen', side_effect=popen), \
                    patch.object(helper_entry, 'wait_for_child_ready', return_value=True), \
                    patch.object(helper_entry, 'stop_update_core'), patch.object(tx, 'recover', side_effect=checked_recover):
                helper_entry.run_helper(str(path))
            self.assertTrue(child.stopped)
            self.assertTrue((target/'old').exists())
            self.assertTrue((path.parent/'result.json').is_symlink())

    def test_process_probe_only_confirms_exit_after_absence_is_verified(self):
        failed = subprocess.CompletedProcess([], 1, '', 'fixture')
        with patch.object(process_identity.subprocess, 'run', return_value=failed):
            for effect in (None, PermissionError('fixture')):
                with self.subTest(effect=effect), patch.object(process_identity.os, 'kill', side_effect=effect):
                    with self.assertRaises(UpdateError): process_identity.process_identity(42)
            with patch.object(process_identity.os, 'kill', side_effect=ProcessLookupError()):
                self.assertIsNone(process_identity.process_identity(42))
        malformed = subprocess.CompletedProcess([], 0, 'incomplete', '')
        with patch.object(process_identity.subprocess, 'run', return_value=malformed):
            with self.assertRaises(UpdateError): process_identity.process_identity(42)

    def test_real_process_identity_preserves_long_executable_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder).resolve()/('long application name '*8)/'Lulu.app/Contents/MacOS/Lulu'
            executable.parent.mkdir(parents=True); shutil.copy('/bin/sleep', executable)
            child = subprocess.Popen([str(executable), '20'])
            try:
                started, actual = process_identity.process_identity(child.pid)
                self.assertEqual(Path(actual).resolve(), executable)
                self.assertTrue(process_identity.matches_process(child.pid, started, str(executable)))
                self.assertTrue(restart_recovery.running_app(executable.parents[2]))
                self.assertFalse(restart_recovery.running_app(executable.parents[2], exclude_pid=child.pid))
            finally:
                child.terminate(); child.wait(timeout=5)
            self.assertIsNone(process_identity.process_identity(child.pid))

    def test_recovery_query_failure_preserves_app_lock_and_visible_error(self):
        with tempfile.TemporaryDirectory() as folder:
            tx, target, path, _ = self.transaction(Path(folder).resolve())
            with patch.object(restart_recovery, 'InstallTransaction', return_value=tx), \
                    patch.object(restart_recovery, 'matches_process', return_value=False), \
                    patch.object(restart_recovery.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ps', 5)), \
                    patch.object(helper_entry, 'stop_update_core') as stop, \
                    patch.object(restart_recovery.subprocess, 'Popen') as launch:
                self.assertEqual(restart_recovery.run_recovery(str(path), 999999, 'fixture', '/fixture'), 5)
            stop.assert_not_called(); launch.assert_not_called()
            self.assertTrue((target/'old').exists()); self.assertTrue(tx.lock.exists())
            self.assertEqual(StateStore(tx.root).load().error_code, 'process_check')

    def test_running_app_rejects_unknown_list_and_keeps_space_paths(self):
        target = Path('/temporary/long application path/Lulu.app')
        command = str(target/'Contents/MacOS/Lulu')
        for output in ('', 'not a pid', '42'):
            with self.subTest(output=output), patch.object(restart_recovery.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, output, '')):
                with self.assertRaises(UpdateError): restart_recovery.running_app(target)
        with patch.object(restart_recovery.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 0, '42 '+command+'\n43 /bin/ps\n', '')):
            self.assertTrue(restart_recovery.running_app(target))
            self.assertFalse(restart_recovery.running_app(target, exclude_pid=42))

if __name__=='__main__': unittest.main()
