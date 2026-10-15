"""Restart and retry checks; only temporary bundles and injected HTTP responses."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.updater.models import UpdateSnapshot
from src.core.updater.service import UpdateService
from src.core.updater.state import StateStore
from src.core.updater.manifest import parse_manifest
from tests.test_auto_update import _manifest, _fixture_zip, DownloadClient
from tests import test_auto_update as fixtures
from src.core.updater.install import InstallTransaction
from src.core.updater.archive import app_file_manifest


class UpdateRecoveryTests(unittest.TestCase):
    def test_update_outcomes_preserve_user_files_and_reopen_existing_document(self):
        from src.config import AppConfig
        from src.core import library
        from src.core.agent_workspace.service import WorkspaceService
        for outcome in ('success', 'rollback', 'after_exchange', 'after_confirmation'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                data = root / 'home/Library/Application Support/Lulu'
                with patch.object(library, 'LIBRARY_DIR', data / 'library'), \
                     patch.object(library, 'FOLDERS_FILE', data / 'library/folders.json'), \
                     patch.object(AppConfig, 'load', return_value=AppConfig()):
                    eid = library.save_document('更新前的原文', '保留文稿')
                    library.save_markdown(eid, '更新前的最后修订🙂')
                    service = WorkspaceService()
                    service.bootstrap()
                    service.view_state('selected_document', eid)
                    for name in ('library/audio/source.wav', 'models/model.bin', 'config.json',
                                 'tasks/history.json', 'library/voices/original.wav'):
                        sample = data / name
                        sample.parent.mkdir(parents=True, exist_ok=True)
                        sample.write_bytes(('保护样本:' + name).encode())
                    before = {str(p.relative_to(data)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in data.rglob('*') if p.is_file()}
                    _, target, staged = fixtures.AutoUpdateTests._apps(self, root)
                    tx = InstallTransaction(root / 'updates', allowed_targets={target}, verify_system=False)
                    path, _ = tx.create_plan(target=target, staged_app=staged, version='0.2.4', build='20260712.1',
                        old_pid=1, old_pid_start_time='fixture', old_executable_path='/fixture',
                        staged_sha=app_file_manifest(staged)[1])
                    result = tx.execute_local(path, launch=lambda *_: None,
                        wait_ready=lambda *_: outcome != 'rollback', old_pid_validator=lambda *_: True,
                        crash_at=outcome if outcome.startswith('after_') else '')
                    if result == 'crashed': result = tx.recover(path)
                    self.assertEqual(result, 'installed' if outcome in {'success', 'after_confirmation'} else 'rolled_back')
                    after = {str(p.relative_to(data)): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in data.rglob('*') if p.is_file()}
                    self.assertEqual(after, before)
                    reopened = WorkspaceService().bootstrap()['screen']
                    self.assertEqual([row['id'] for row in reopened['listing']['entries']], [eid])
                    self.assertEqual(reopened['content']['document']['text'], '更新前的最后修订🙂')

    def test_result_is_durable_before_install_lock_disappears(self):
        for ready in (True, False):
            with self.subTest(ready=ready), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                _, target, staged = fixtures.AutoUpdateTests._apps(self, root)
                tx = InstallTransaction(root / 'updates', allowed_targets={target}, verify_system=False)
                path, plan = tx.create_plan(target=target, staged_app=staged, version='0.2.4', build='20260712.1',
                    old_pid=1, old_pid_start_time='fixture', old_executable_path='/fixture',
                    staged_sha=app_file_manifest(staged)[1])
                store = StateStore(root / 'updates')
                store.save(UpdateSnapshot(state='installing', ui_state='preparing', txn=plan['txn']))
                original_release = tx.release
                expected = 'installed' if ready else 'install_error_rollback'
                def release(**kwargs):
                    self.assertEqual(store.load().ui_state, expected)
                    original_release(**kwargs)
                with patch.object(tx, 'release', side_effect=release):
                    tx.execute_local(path, launch=lambda *_: None, wait_ready=lambda *_: ready,
                                     old_pid_validator=lambda *_: True)
                self.assertEqual(UpdateService(root=store.root, public_keys={}).snapshot.ui_state, expected)

    def test_legacy_install_gap_remains_actionable_and_accepts_final_helper_result(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp))
            store.save(UpdateSnapshot(state='installing', ui_state='preparing', txn='a' * 64))
            service = UpdateService(root=store.root, public_keys={})
            self.assertEqual(service.snapshot.ui_state, 'install_interrupted')
            store.save(UpdateSnapshot(state='installed', ui_state='installed', txn='a' * 64))
            self.assertTrue(service.refresh_external())
            self.assertEqual(service.snapshot.ui_state, 'installed')


    def test_recovery_child_waits_for_dispatcher_to_release_guard(self):
        from src.core.updater import restart_recovery as recovery
        with tempfile.TemporaryDirectory() as temp:
            root = StateStore(Path(temp)).root
            with patch.object(recovery.fcntl, 'flock', side_effect=[BlockingIOError(), None]) as lock, \
                 patch.object(recovery.time, 'sleep'):
                with recovery.helper_guard(root, wait_seconds=2) as acquired:
                    self.assertTrue(acquired)
            self.assertEqual(lock.call_count, 2)

    def test_atomic_swap_always_leaves_launchable_path_at_every_interruption(self):
        for point in ('after_copy', 'after_exchange', 'after_backup', 'after_install'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                _, target, staged = fixtures.AutoUpdateTests._apps(self, root)
                tx = InstallTransaction(root / 'updates', allowed_targets={target}, verify_system=False)
                path, _ = tx.create_plan(target=target, staged_app=staged, version='0.2.4', build='20260712.1',
                    old_pid=1, old_pid_start_time='fixture', old_executable_path='/fixture',
                    staged_sha=app_file_manifest(staged)[1])
                result = tx.execute_local(path, launch=lambda *_: None, wait_ready=lambda *_: False,
                                          old_pid_validator=lambda *_: True, crash_at=point)
                self.assertEqual(result, 'crashed')
                self.assertTrue((target / 'Contents/MacOS/Lulu').is_file())
                self.assertEqual(tx.recover(path), 'rolled_back')
                self.assertTrue((target / 'old').is_file())


    def test_completed_confirmation_survives_helper_interruption(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, target, staged = fixtures.AutoUpdateTests._apps(self, root)
            tx = InstallTransaction(root / 'updates', allowed_targets={target}, verify_system=False)
            path, _ = tx.create_plan(target=target, staged_app=staged, version='0.2.4', build='20260712.1',
                old_pid=1, old_pid_start_time='fixture', old_executable_path='/fixture',
                staged_sha=app_file_manifest(staged)[1])
            self.assertEqual(tx.execute_local(path, launch=lambda *_: None, wait_ready=lambda *_: True,
                             old_pid_validator=lambda *_: True, crash_at='after_confirmation'), 'crashed')
            self.assertEqual(tx.recover(path), 'installed')
            self.assertTrue((target / 'new').exists())


    def test_normal_start_routes_abandoned_transaction_to_recovery_child(self):
        from src.core.updater import restart_recovery as recovery
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, target, staged = fixtures.AutoUpdateTests._apps(self, root)
            tx = InstallTransaction(root / 'updates', allowed_targets={target}, verify_system=False)
            path, _ = tx.create_plan(target=target, staged_app=staged, version='0.2.4', build='20260712.1',
                old_pid=1, old_pid_start_time='fixture', old_executable_path='/fixture',
                staged_sha=app_file_manifest(staged)[1])
            tx.execute_local(path, launch=lambda *_: None, wait_ready=lambda *_: False,
                             old_pid_validator=lambda *_: True, crash_at='after_exchange')
            with patch.object(recovery, 'InstallTransaction', return_value=tx), \
                 patch.object(recovery, 'matches_process', return_value=False), \
                 patch.object(recovery, 'current_identity', return_value=('start', '/fixture')), \
                 patch.object(recovery, 'running_app', return_value=False) as running, \
                 patch.object(recovery.subprocess, 'Popen') as spawn:
                self.assertTrue(recovery.launch_pending_recovery(root=root / 'updates', executable=target/'Contents/MacOS/Lulu'))
                self.assertIn('--lulu-update-recover', spawn.call_args.args[0])
                spawn.reset_mock()
                running.return_value = True
                self.assertTrue(recovery.launch_pending_recovery(root=root/'updates', executable=target/'Contents/MacOS/Lulu'))
                spawn.assert_not_called()
                running.return_value = False
                with recovery.helper_guard(root/'updates'):
                    with patch.object(recovery.time, 'monotonic', side_effect=[0, 3]):
                        self.assertTrue(recovery.launch_pending_recovery(root=root/'updates', executable=target/'Contents/MacOS/Lulu'))
                spawn.assert_not_called()
                with patch('src.core.updater.helper_entry.stop_update_core'):
                    self.assertEqual(recovery.run_recovery(str(path), 999999, 'start', '/fixture'), 0)
                self.assertTrue((target/'old').exists())
                self.assertEqual(StateStore(root/'updates').load().ui_state, 'install_error_rollback')

    def test_finishing_helper_allows_normal_reopen_without_corrupt_result(self):
        from contextlib import contextmanager
        from src.core.updater import restart_recovery as recovery
        with tempfile.TemporaryDirectory() as temp:
            root = StateStore(Path(temp)).root
            lock = root/'transactions/update.lock'
            lock.write_text('a'*64)
            @contextmanager
            def finishing_helper(_root, *, wait_seconds):
                self.assertEqual(wait_seconds, 2)
                lock.unlink()  # The live helper durably completed while waiting.
                yield True
            with patch.object(recovery, 'helper_guard', finishing_helper), \
                 patch.object(recovery, 'save_result') as save, \
                 patch.object(recovery.subprocess, 'Popen') as launch:
                self.assertFalse(recovery.launch_pending_recovery(root=root))
            save.assert_not_called(); launch.assert_not_called()


    def test_paused_restart_rechecks_before_downloading_and_resumes_same_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            data = b'a' * (1024 * 1024) + b'xyz'
            size = len(data)
            service = UpdateService(root=Path(temp), public_keys={})
            manifest = parse_manifest(json.dumps(_manifest(hashlib.sha256(data).hexdigest(), len(data))).encode(), local_channel="trusted-test")
            service.manifest = manifest
            service.snapshot.verified_manifest = True
            service.client = DownloadClient([(200, {'etag':'same', 'content-length':str(size)}, data)])
            with self.assertRaisesRegex(Exception, '暂停'):
                service.download(cancelled=lambda: service.snapshot.downloaded >= 1024 * 1024)
            reopened = UpdateService(root=Path(temp), public_keys={})
            reopened.client = DownloadClient([(206, {'etag':'same', 'content-range':f'bytes {size-3}-{size-1}/{size}'}, b'xyz')])
            def check(*, manual):
                reopened.manifest = manifest
                return reopened._save(verified_manifest=True, ui_state='update_available', update_available=True)
            with patch.object(reopened, 'check', side_effect=check) as checked:
                result = reopened.download()
            checked.assert_called_once_with(manual=True)
            self.assertEqual(result.read_bytes(), data)
            self.assertEqual(reopened.client.headers[0]['Range'], f'bytes={size-3}-')

    def test_each_stage_uses_a_new_transaction_and_preserves_previous_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / 'input.zip'
            _fixture_zip(archive)
            from src.core.updater.archive import extract_zip, app_file_manifest
            app = extract_zip(archive, root / 'measure')
            raw = _manifest(hashlib.sha256(archive.read_bytes()).hexdigest(), archive.stat().st_size)
            raw['assets']['macos_arm64']['tree_manifest_sha256'] = app_file_manifest(app)[1]
            service = UpdateService(root=root / 'updates', public_keys={})
            service.manifest = parse_manifest(json.dumps(raw).encode(), local_channel="trusted-test")
            service.snapshot.verified_manifest = True
            first = service.stage(archive, verify_system=False)
            second = service.stage(archive, verify_system=False)
            self.assertNotEqual(first, second)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())


    def test_manual_upgrade_retires_old_failure_and_ready_cache_without_touching_active_lock(self):
        from src.core.updater import service as module
        for status,ui in [('failed','rollback_failed'),('ready_to_install','ready'),('download_paused','download_paused')]:
            for locked in (False,True):
                with self.subTest(status=status,locked=locked), tempfile.TemporaryDirectory() as temp:
                    root=Path(temp).resolve();store=StateStore(root)
                    store.save(UpdateSnapshot(state=status,ui_state=ui,current_version='4.1.4',current_build='20261011.4',latest_version='4.1.5',latest_build='20261011.5',staged_app='/old/staged/Lulu.app',staged_manifest_sha256='a'*64,verified_manifest=True,update_available=True,error_code='old-failure'))
                    if locked:(root/'transactions/update.lock').write_text('b'*64+'\n')
                    with patch.object(module,'__version__','4.1.6'),patch.object(module,'__build__','20261012.2'):
                        snapshot=UpdateService(root=root,public_keys={}).snapshot
                    if locked:
                        self.assertEqual((snapshot.state,snapshot.staged_app),(status,'/old/staged/Lulu.app'))
                    else:
                        self.assertEqual((snapshot.state,snapshot.ui_state),('installed','installed'))
                        self.assertEqual((snapshot.staged_app,snapshot.error_code),('',''))
                        self.assertFalse(snapshot.update_available)
                        self.assertEqual(snapshot.current_version,'4.1.6')

    def test_newer_interrupted_candidate_is_not_discarded_on_normal_reopen(self):
        from src.core.updater import service as module
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();store=StateStore(root)
            store.save(UpdateSnapshot(state='ready_to_install',ui_state='ready',current_version='4.1.6',current_build='20261012.1',latest_version='4.1.6',latest_build='20261012.2',staged_app='/new/staged/Lulu.app'))
            with patch.object(module,'__version__','4.1.6'),patch.object(module,'__build__','20261012.1'):
                snapshot=UpdateService(root=root,public_keys={}).snapshot
            self.assertEqual((snapshot.state,snapshot.staged_app),('ready_to_install','/new/staged/Lulu.app'))

    def test_manual_upgrade_keeps_newer_candidate_and_invalid_ready_requires_recheck(self):
        from src.core.updater import service as module
        for latest,build,expected in [('4.1.7','20261013.1','ready'),('','', 'check_error'),('bad','bad','check_error')]:
            with self.subTest(latest=latest), tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve();store=StateStore(root)
                store.save(UpdateSnapshot(state='ready_to_install',ui_state='ready',current_version='4.1.4',current_build='20261011.4',latest_version=latest,latest_build=build,staged_app='/staged/Lulu.app',total=99,downloaded=99))
                with patch.object(module,'__version__','4.1.6'),patch.object(module,'__build__','20261012.2'):
                    snapshot=UpdateService(root=root,public_keys={}).snapshot
                self.assertEqual(snapshot.ui_state,expected)
                if expected=='check_error':
                    self.assertEqual((snapshot.staged_app,snapshot.total,snapshot.downloaded),('',0,0))


if __name__ == '__main__':
    unittest.main()
