"""Real-failure regressions with temporary Apps and a virtual clock, no Keychain."""
import errno
from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.core.updater.archive import app_file_manifest, extract_zip
from src.core.updater.cache_cleanup import cleanup_successful_update
from src.core.updater.cleanup import cleanup_stale_on_startup
from src.core.updater.install import InstallTransaction
from src.core.updater.manifest import parse_manifest
from src.core.updater.models import UpdateError, UpdateSnapshot
from src.core.updater.service import UpdateService
from src.core.updater.state import StateStore
from tests.test_auto_update import _fixture_zip, _manifest
from tests import test_auto_update as fixtures
from tests.test_update_child_readiness import Clock


class UpdateAuthorizationRetryTests(unittest.TestCase):
    def make_staged(self, root, txn):
        app = root / 'staged' / txn / 'Lulu.app'
        framework = app / 'Contents/Frameworks/Python.framework'
        payload = framework / 'Versions/A/Python'
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b'framework')
        (framework / 'Versions/Current').symlink_to('A')
        (framework / 'Python').symlink_to('Versions/Current/Python')
        (app / 'Contents/dangling').symlink_to('already-deleted-by-legacy-cleanup')
        outside = root.parent / 'personal-library'
        outside.mkdir(exist_ok=True)
        (outside / 'keep.md').write_text('用户原文稿')
        (app / 'Contents/external').symlink_to(outside, target_is_directory=True)
        return app, outside

    def test_startup_removes_legacy_link_skeleton_without_following_any_target(self):
        for old_cleanup in (False, True):
            with self.subTest(old_cleanup=old_cleanup), tempfile.TemporaryDirectory() as temp:
                store = StateStore(Path(temp) / 'updates')
                app, outside = self.make_staged(store.root, 'a' * 64)
                if old_cleanup:
                    (app / 'Contents/Frameworks/Python.framework/Versions/A/Python').unlink()
                result = cleanup_stale_on_startup(store.root, UpdateSnapshot(ui_state='install_error_rollback'))
                self.assertEqual(result['result'], 'completed')
                self.assertEqual(result['blocked'], [])
                self.assertEqual(list((store.root / 'staged').iterdir()), [])
                self.assertEqual((outside / 'keep.md').read_text(), '用户原文稿')

    def test_success_cleanup_removes_framework_links_only_in_current_staged_app(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / 'updates')
            app, outside = self.make_staged(store.root, 'a' * 64)
            other, _ = self.make_staged(store.root, 'b' * 64)
            receipt = cleanup_successful_update(store.root, {'txn': 'a' * 64})
            self.assertGreater(receipt['staged_removed'], 0)
            self.assertFalse(app.parent.exists())
            self.assertTrue((other / 'Contents/Frameworks/Python.framework/Python').is_symlink())
            self.assertEqual((outside / 'keep.md').read_text(), '用户原文稿')

    def test_cleanup_keeps_links_that_replace_transaction_or_app_root(self):
        for level in ('transaction', 'app'):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as temp:
                store = StateStore(Path(temp) / 'updates')
                outside = Path(temp) / 'personal-library'
                outside.mkdir()
                (outside / 'keep.md').write_text('keep')
                link = store.root / 'staged' / ('a' * 64)
                if level == 'app':
                    link.mkdir()
                    link = link / 'Lulu.app'
                link.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(UpdateError):
                    cleanup_successful_update(store.root, {'txn': 'a' * 64})
                result = cleanup_stale_on_startup(store.root, UpdateSnapshot())
                self.assertEqual(result['result'], 'partial')
                self.assertTrue(result['blocked'])
                self.assertTrue(link.is_symlink())
                self.assertEqual((outside / 'keep.md').read_text(), 'keep')

    def test_retry_after_legacy_rollback_cleans_skeleton_and_uses_fresh_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = StateStore(root / 'updates')
            old, outside = self.make_staged(store.root, 'a' * 64)
            store.save(UpdateSnapshot(state='failed_security_rejected', ui_state='zip_structure_error',
                                      error_code='staged_exists', txn='a' * 64, staged_app=str(old)))
            service = UpdateService(root=store.root, public_keys={})
            self.assertFalse(old.parent.exists())
            archive = root / 'release.zip'
            _fixture_zip(archive)
            expected = extract_zip(archive, root / 'expected')
            manifest = _manifest()
            manifest['assets']['macos_arm64']['tree_manifest_sha256'] = app_file_manifest(expected)[1]
            service.manifest = parse_manifest(json.dumps(manifest).encode(), local_channel='trusted-test')
            service.snapshot.verified_manifest = True
            staged = service.stage(archive, verify_system=False)
            self.assertNotEqual(staged.parent.name, 'a' * 64)
            self.assertEqual(service.snapshot.ui_state, 'ready')
            self.assertEqual((outside / 'keep.md').read_text(), '用户原文稿')

    def test_stage_filesystem_errors_are_not_reported_as_broken_downloads(self):
        cases = ((UpdateError('staged_exists', 'fixture'), 'staged_exists'),
                 (OSError(errno.ENOSPC, 'fixture'), 'storage_full'),
                 (PermissionError(errno.EACCES, 'fixture'), 'staging_io'))
        for error, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                service = UpdateService(root=Path(temp), public_keys={})
                service.manifest = parse_manifest(json.dumps(_manifest()).encode(), local_channel='trusted-test')
                service.snapshot.verified_manifest = True
                with patch('src.core.updater.service.extract_zip', side_effect=error):
                    with self.assertRaises(UpdateError):
                        service.stage(Path(temp) / 'fixture.zip', verify_system=False)
                self.assertEqual(service.store.load().ui_state, 'preparation_error')
                self.assertEqual(service.store.load().error_code, code)
                self.assertEqual(service.snapshot.staged_app, '')

    def test_new_helper_accepts_delayed_authorization_and_keeps_backup_until_ready(self):
        from src.core.updater.helper_entry import wait_for_child_ready
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, target, staged = fixtures.AutoUpdateTests._apps(self, root)
            tx = InstallTransaction(root / 'updates', allowed_targets={target}, verify_system=False)
            path, plan = tx.create_plan(target=target, staged_app=staged, version='0.2.4', build='20260712.1',
                old_pid=1, old_pid_start_time='fixture', old_executable_path='/fixture',
                staged_sha=app_file_manifest(staged)[1])
            clock = Clock()
            child = Mock()
            child.poll.return_value = None
            ready_path = Mock(spec=Path)
            ready_path.exists.side_effect = lambda: clock.now >= 90
            ready_path.is_symlink.return_value = False
            verifier = Mock()
            verifier.validate_ready.return_value = True
            def wait(_path, current_plan, timeout):
                self.assertTrue((Path(current_plan['backup_app']) / 'old').exists())
                return wait_for_child_ready(verifier, ready_path, current_plan, child, None, timeout=timeout)
            def advance(seconds):
                self.assertTrue((Path(plan['backup_app']) / 'old').exists())
                clock.sleep(seconds)
            with patch('src.core.updater.helper_entry.time.monotonic', clock.monotonic), \
                 patch('src.core.updater.helper_entry.time.sleep', advance):
                result = tx.execute_local(path, launch=lambda *_: None, wait_ready=wait,
                                          old_pid_validator=lambda *_: True)
            self.assertEqual(result, 'installed')
            self.assertGreaterEqual(clock.now, 98)
            self.assertTrue((target / 'new').exists())
            self.assertFalse(Path(plan['backup_app']).exists())
            child.terminate.assert_not_called()

    def test_actual_disk_full_keeps_original_error_and_closes_extraction_files(self):
        from src.core.updater import archive as archive_module
        for during, cleanup_fails in (('write', False), ('flush', False), ('write', True)):
            with self.subTest(during=during, cleanup_fails=cleanup_fails), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                release = root / 'release.zip'
                _fixture_zip(release)
                expected = extract_zip(release, root / 'expected')
                manifest = _manifest()
                manifest['assets']['macos_arm64']['tree_manifest_sha256'] = app_file_manifest(expected)[1]
                service = UpdateService(root=root / 'updates', public_keys={})
                service.manifest = parse_manifest(json.dumps(manifest).encode(), local_channel='trusted-test')
                service.snapshot.verified_manifest = True
                real_fdopen, real_open, opened = os.fdopen, os.open, []
                class DiskFullFile:
                    def __init__(self, stream): self.stream = stream
                    def __enter__(self): self.stream.__enter__(); return self
                    def __exit__(self, *args):
                        result = self.stream.__exit__(*args)
                        if during == 'flush' and args[0] is None:
                            raise OSError(errno.ENOSPC, 'isolated flush failure')
                        return result
                    def write(self, block):
                        if during == 'write':
                            raise OSError(errno.ENOSPC, 'isolated write failure')
                        return self.stream.write(block)
                def destination(fd, mode='r', *args, **kwargs):
                    stream = real_fdopen(fd, mode, *args, **kwargs)
                    return DiskFullFile(stream) if mode == 'wb' else stream
                def track_open(*args, **kwargs):
                    fd = real_open(*args, **kwargs)
                    opened.append(fd)
                    return fd
                def extract(*args, **kwargs):
                    cleanup = (patch.object(archive_module, '_remove_staged', side_effect=PermissionError('isolated cleanup failure'))
                               if cleanup_fails else nullcontext())
                    with patch.object(os, 'fdopen', destination), patch.object(os, 'open', track_open), cleanup:
                        return extract_zip(*args, **kwargs)
                with patch('src.core.updater.service.extract_zip', extract):
                    with self.assertRaises(UpdateError) as caught:
                        service.stage(release, verify_system=False)
                self.assertEqual(caught.exception.code, 'storage_full')
                self.assertEqual(service.store.load().error_code, 'storage_full')
                for fd in set(opened):
                    with self.assertRaises(OSError):
                        os.fstat(fd)
                self.assertEqual((service.store.root / 'staged' / service.snapshot.txn).exists(), cleanup_fails)

    def test_authorization_timeout_still_stops_child_and_restores_old_app(self):
        from src.core.updater.helper_entry import wait_for_child_ready
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, target, staged = fixtures.AutoUpdateTests._apps(self, root)
            tx = InstallTransaction(root / 'updates', allowed_targets={target}, verify_system=False)
            path, _ = tx.create_plan(target=target, staged_app=staged, version='0.2.4', build='20260712.1',
                old_pid=1, old_pid_start_time='fixture', old_executable_path='/fixture',
                staged_sha=app_file_manifest(staged)[1])
            clock, child, ready_path = Clock(), Mock(), Mock(spec=Path)
            child.poll.return_value = None
            ready_path.exists.return_value = ready_path.is_symlink.return_value = False
            def wait(_path, plan, timeout):
                return wait_for_child_ready(Mock(), ready_path, plan, child, None, timeout=timeout)
            with patch('src.core.updater.helper_entry.time.monotonic', clock.monotonic), \
                 patch('src.core.updater.helper_entry.time.sleep', clock.sleep):
                result = tx.execute_local(path, launch=lambda *_: None, wait_ready=wait,
                                          old_pid_validator=lambda *_: True)
            self.assertEqual(result, 'rolled_back')
            self.assertGreaterEqual(clock.now, 300)
            self.assertLess(clock.now, 361)
            self.assertTrue((target / 'old').exists())
            self.assertFalse((target / 'new').exists())
            child.terminate.assert_called_once_with()
            child.wait.assert_called_once_with(timeout=5)

    @patch('src.core.updater.helper_entry.PreSwapHandover')
    def test_real_helper_launch_advertises_long_wait_to_child(self, _handover):
        from src.core.updater import helper_entry
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'updates/transactions' / ('a' * 64) / 'install_plan.json'
            path.parent.mkdir(parents=True)
            target = root / 'Applications/Lulu.app'
            executable = str(target / 'Contents/MacOS/Lulu')
            plan = dict(txn='a' * 64, target_app=str(target), old_pid=1,
                        staged_app=str(root), target_parent=str(root))
            tx, store, child = Mock(), Mock(), Mock(pid=42)
            tx.load_plan.return_value = plan
            store.load.return_value = UpdateSnapshot(state='installed', ui_state='installed')
            child.poll.return_value = None
            def execute(_path, **kwargs):
                self.assertEqual(kwargs['launch'](target, plan), (42, 'start', executable))
                return 'installed'
            tx.execute_local.side_effect = execute
            with patch.object(helper_entry, 'InstallTransaction', return_value=tx), \
                 patch.object(helper_entry, 'StateStore', return_value=store), \
                 patch.object(helper_entry, 'process_identity', side_effect=lambda pid: None if pid == 1 else ('start', executable)), \
                 patch.object(helper_entry, 'cleanup_successful_update'), \
                 patch.object(helper_entry.subprocess, 'Popen', return_value=child) as spawn:
                self.assertEqual(helper_entry._run_helper(str(path)), 0)
            self.assertEqual(spawn.call_args.args[0],
                             [executable, '--lulu-update-ready', str(path.resolve()), '--lulu-update-authorization-wait'])
