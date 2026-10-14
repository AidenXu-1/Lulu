"""Real private transaction files; no real App, process, service or credentials."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.core.updater import launch, helper_entry, helper_protocol
from src.core.updater.install import InstallTransaction
from src.core.updater.models import UpdateSnapshot
from src.core.updater.state import StateStore
from src.core.updater.archive import app_file_manifest
from tests import test_auto_update as fixtures


class ReachedHandover(BaseException):
    pass


class UpdateLaunchTests(unittest.TestCase):
    def setUp(self):
        version = patch.object(launch,'__version__','0.2.3');version.start();self.addCleanup(version.stop)
        build = patch.object(launch,'__build__','20260711.1');build.start();self.addCleanup(build.stop)
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        _, self.target, self.staged = fixtures.AutoUpdateTests._apps(self, self.root)
        self.executable = self.target/'Contents/MacOS/Lulu'
        self.tx = InstallTransaction(self.root/'updates', allowed_targets={self.target}, verify_system=False)
        self.snapshot = UpdateSnapshot(state='staged', ui_state='ready', staged_app=str(self.staged),
            latest_version='0.2.4', latest_build='20260712.1', staged_manifest_sha256=app_file_manifest(self.staged)[1])
        self.store = StateStore(self.tx.root)
        self.store.save(self.snapshot)

    def plan(self, protocol=False):
        path, plan = self.tx.create_plan(target=self.target, staged_app=self.staged,
            version=self.snapshot.latest_version, build=self.snapshot.latest_build,
            old_pid=123, old_pid_start_time='start', old_executable_path=str(self.executable),
            staged_sha=self.snapshot.staged_manifest_sha256, launch_protocol=protocol)
        self.store.save(UpdateSnapshot(txn=plan['txn'],state='installing',ui_state='preparing'))
        return path, plan

    def test_initial_state_precedes_spawn_and_fast_failure_is_not_overwritten(self):
        child = Mock(pid=234); child.poll.return_value=3
        def spawn(args, **kwargs):
            path=Path(args[-1]); plan=self.tx.load_plan(path)
            state=self.store.load()
            self.assertEqual((state.txn,state.state),(plan['txn'],'installing'))
            self.assertEqual(plan['launch_protocol'],1)
            self.assertEqual(kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'],'1')
            helper_protocol.fail_before_swap(path,plan,self.tx,'old_process_changed',3)
            return child
        with patch.object(launch,'InstallTransaction',return_value=self.tx), \
             patch.object(launch,'current_identity',return_value=('start',str(self.executable))), \
             patch.object(launch.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'apfs','')), \
             patch.object(launch.subprocess,'Popen',side_effect=spawn):
            session=launch.prepare_launch(self.executable,self.snapshot,self.tx.root)
            self.assertEqual(session.poll(),'failed')
        self.assertEqual(self.store.load().error_code,'old_process_changed')
        self.assertEqual(self.store.load().ui_state,'preparation_error')

    def test_acceptance_timeout_waits_for_owned_child_to_exit(self):
        path,plan=self.plan();child=Mock(pid=234);child.poll.return_value=None
        session=launch.InstallLaunch(self.tx,path,plan,child,0)
        with patch.object(launch,'accepted',return_value=False):
            self.assertEqual(session.poll(),'waiting')
            child.terminate.assert_called_once()
            self.assertTrue(self.tx.lock.exists())
            child.poll.return_value=-15
            self.assertEqual(session.poll(),'failed')
        self.assertFalse(self.tx.lock.exists())
        self.assertEqual(self.store.load().error_code,'helper_accept_timeout')

    def test_ack_is_written_before_waiting_for_gui_to_exit(self):
        path,plan=self.plan(protocol=True)
        handover=Mock();handover.prepare.side_effect=ReachedHandover()
        observed=[]
        def sleep(_): observed.append((path.parent/'helper-accepted.json').is_file())
        with patch.object(helper_entry,'InstallTransaction',return_value=self.tx), \
             patch.object(helper_entry,'process_identity',side_effect=[('start',str(self.executable)),None]), \
             patch.object(helper_protocol,'current_identity',return_value=('helper-start',str(self.executable))), \
             patch.object(helper_entry.time,'sleep',side_effect=sleep), \
             patch.object(helper_entry,'PreSwapHandover',return_value=handover):
            with self.assertRaises(ReachedHandover):helper_entry._run_helper(str(path))
        self.assertEqual(observed,[True])

    def test_normal_exit_uses_one_observation_per_iteration(self):
        path,plan=self.plan();handover=Mock();handover.prepare.side_effect=ReachedHandover()
        with patch.object(helper_entry,'InstallTransaction',return_value=self.tx), \
             patch.object(helper_entry,'process_identity',side_effect=[('start',str(self.executable)),None]) as observe, \
             patch.object(helper_entry.time,'sleep'), patch.object(helper_entry,'PreSwapHandover',return_value=handover):
            with self.assertRaises(ReachedHandover):helper_entry._run_helper(str(path))
        self.assertEqual(observe.call_count,2)
        self.assertTrue(self.tx.lock.exists())

    def test_real_identity_parser_allows_exit_transition_before_handover(self):
        path, plan = self.plan()
        handover = Mock(); handover.prepare.side_effect = ReachedHandover()
        observations = [subprocess.CompletedProcess([], 0, line, '') for line in (
            '?E Mon Oct 12 12:43:21 2026 (sleep)\n',
            'Z Mon Oct 12 12:43:21 2026 <defunct>\n')]
        with patch.object(helper_entry, 'InstallTransaction', return_value=self.tx), \
                patch.object(helper_entry.subprocess, 'run', side_effect=observations), \
                patch.object(helper_entry.time, 'sleep'), \
                patch.object(helper_entry, 'PreSwapHandover', return_value=handover):
            with self.assertRaises(ReachedHandover): helper_entry._run_helper(str(path))
        self.assertTrue(self.tx.lock.exists())
        self.assertEqual(self.store.load().state, 'installing')

    def test_live_identity_change_is_recorded_without_handover(self):
        path,plan=self.plan()
        with patch.object(helper_entry,'InstallTransaction',return_value=self.tx), \
             patch.object(helper_entry,'process_identity',return_value=('different',str(self.executable))), \
             patch.object(helper_entry,'PreSwapHandover') as handover:
            self.assertEqual(helper_entry._run_helper(str(path)),3)
        handover.assert_not_called()
        self.assertEqual(self.store.load().error_code,'old_process_changed')
        self.assertFalse(self.tx.lock.exists())

    def test_plan_write_failure_leaves_no_lock_and_can_retry(self):
        from src.core.updater import install, restart_recovery
        with patch.object(install, '_write_json_exclusive_at', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.plan()
        self.assertFalse(self.tx.lock.exists())
        self.assertFalse(restart_recovery.launch_pending_recovery(root=self.tx.root, executable=self.executable))
        path, plan = self.plan()
        self.assertTrue(path.is_file())
        self.assertTrue(self.tx.owns(plan['txn']))

    def test_rollback_failure_keeps_lock_and_next_recovery_restores_old_app(self):
        from src.core.updater import install
        path, plan = self.plan()
        original = install._exchange_apps
        attempts = []
        def exchange(*args):
            attempts.append(1)
            if len(attempts) == 2: raise OSError('temporary IO failure')
            return original(*args)
        with patch.object(install, '_exchange_apps', side_effect=exchange):
            self.assertEqual(self.tx.execute_local(path, launch=lambda *_: None,
                wait_ready=lambda *_: False, old_pid_validator=lambda *_: True), 'failed')
        self.assertTrue(self.tx.lock.exists())
        self.assertTrue((Path(plan['backup_app'])/'old').exists())
        self.assertEqual(self.tx.recover(path), 'rolled_back')
        self.assertTrue((self.target/'old').exists())
        self.assertFalse(self.tx.lock.exists())

    def test_recovery_write_failure_is_retryable_even_after_old_app_restored(self):
        path, plan = self.plan()
        self.assertEqual(self.tx.execute_local(path, launch=lambda *_: None,
            wait_ready=lambda *_: False, old_pid_validator=lambda *_: True,
            crash_at='after_install'), 'crashed')
        with patch.object(StateStore, 'record_result', side_effect=OSError('disk full')):
            self.assertEqual(self.tx.recover(path), 'failed')
        self.assertTrue(self.tx.lock.exists())
        self.assertTrue((self.target/'old').exists())
        self.assertEqual(self.tx.recover(path), 'rolled_back')
        self.assertFalse(self.tx.lock.exists())
        self.assertEqual(self.store.load().ui_state, 'install_error_rollback')

    def test_lock_write_failure_never_publishes_partial_owner(self):
        from src.core.updater import install
        with patch.object(install.os, 'fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.tx.acquire('d'*64)
        self.assertFalse(self.tx.lock.exists())
        self.tx.acquire('d'*64)
        self.assertTrue(self.tx.owns('d'*64))

    def test_corrupt_plan_keeps_owner_and_recovery_is_rejected(self):
        path, plan = self.plan()
        path.write_text('{}')
        self.assertEqual(self.tx.recover(path), 'failed_state_corrupt')
        self.assertTrue(self.tx.owns(plan['txn']))

    def test_helper_final_read_error_does_not_erase_failed_rollback_recovery(self):
        from src.core.updater import install
        path, plan = self.plan()
        original_exchange, original_execute = install._exchange_apps, self.tx.execute_local
        calls = []
        def exchange(*args):
            calls.append(1)
            if len(calls) == 2: raise OSError('rollback IO failure')
            return original_exchange(*args)
        def execute(*args, **kwargs):
            return original_execute(path, launch=lambda *_: None, wait_ready=lambda *_: False,
                old_pid_validator=lambda *_: True)
        with patch.object(helper_entry,'InstallTransaction', return_value=self.tx), \
             patch.object(helper_entry,'process_identity', return_value=None), \
             patch.object(helper_entry,'PreSwapHandover'), \
             patch.object(self.tx,'execute_local', side_effect=execute), \
             patch.object(install,'_exchange_apps', side_effect=exchange), \
             patch.object(StateStore,'load', side_effect=OSError('read IO failure')):
            self.assertEqual(helper_entry._run_helper(str(path)), 6)
        self.assertTrue(self.tx.owns(plan['txn']))
        self.assertEqual(self.tx.recover(path), 'rolled_back')
        self.assertTrue((self.target/'old').exists())

    def test_cached_same_or_older_target_is_rejected_before_any_spawn_or_lock(self):
        for version,build in [('4.1.5','20990101.1'),('4.1.6','20261012.1'),('4.1.6','20261012.2')]:
            with self.subTest(version=version,build=build):
                self.snapshot.latest_version=version;self.snapshot.latest_build=build
                with patch.object(launch,'__version__','4.1.6',create=True),patch.object(launch,'__build__','20261012.2',create=True),patch.object(launch.subprocess,'Popen') as spawn:
                    with self.assertRaisesRegex(Exception,'当前版本'):
                        launch.prepare_launch(self.executable,self.snapshot,self.tx.root)
                spawn.assert_not_called();self.assertFalse(self.tx.lock.exists())

    def test_low_space_is_rejected_while_old_gui_is_still_running(self):
        from src.core.updater import install
        with patch.object(install.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)), \
             patch.object(launch.subprocess, 'run', return_value=subprocess.CompletedProcess([],0)), \
             patch.object(launch.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(Exception, '安装空间不足'):
                launch.prepare_launch(self.executable,self.snapshot,self.tx.root)
        spawn.assert_not_called()
        self.assertFalse(self.tx.lock.exists())
        self.assertTrue((self.target/'old').exists())
        self.assertEqual(self.store.load().ui_state,'ready')

    def test_space_counts_copied_files_and_reserve_but_not_link_targets(self):
        from src.core.updater import install
        app=self.root/'space-app';app.mkdir();(app/'payload').write_bytes(b'a'*23)
        (app/'alias').symlink_to('payload')
        for extra,passes in ((-1,False),(0,True)):
            with patch.object(install.shutil,'disk_usage',return_value=SimpleNamespace(free=1024**3+23+extra)):
                if passes:install.ensure_install_space(app,self.root)
                else:
                    with self.assertRaisesRegex(Exception,'安装空间不足'):
                        install.ensure_install_space(app,self.root)

    def test_helper_rechecks_space_before_stopping_old_core(self):
        from src.core.updater import install
        path,plan=self.plan(protocol=True)
        with patch.object(helper_entry,'InstallTransaction',return_value=self.tx), \
             patch.object(helper_entry,'process_identity',return_value=None), \
             patch.object(helper_protocol,'current_identity',return_value=('helper-start',str(self.executable))), \
             patch.object(install.shutil,'disk_usage',return_value=SimpleNamespace(free=0)), \
             patch.object(helper_entry,'PreSwapHandover') as handover, \
             patch.object(helper_entry.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'apfs','')), \
             patch.object(helper_entry.subprocess,'Popen') as reopen:
            self.assertEqual(helper_entry._run_helper(str(path)),5)
        handover.assert_not_called();reopen.assert_called_once()
        self.assertFalse(self.tx.lock.exists())
        self.assertEqual(self.store.load().error_code,'install_space')
        self.assertTrue((self.target/'old').exists())

    def test_unexpected_helper_error_preserves_owner_and_reopens_untouched_app(self):
        path,plan=self.plan()
        with patch.object(helper_entry,'InstallTransaction',return_value=self.tx), \
             patch.object(helper_entry,'_run_helper',side_effect=RuntimeError('injected failure')), \
             patch.object(helper_entry,'process_identity',return_value=None), \
             patch.object(helper_entry.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'apfs','')), \
             patch.object(helper_entry.subprocess,'Popen') as reopen:
            self.assertEqual(helper_entry.run_helper(str(path)),5)
        self.assertTrue(self.tx.owns(plan['txn']))
        reopen.assert_called_once()
        self.assertEqual(reopen.call_args.args[0],[str(self.executable)])

    def test_caught_failure_does_not_reopen_changed_app(self):
        path,plan=self.plan()
        old=self.target.with_name('old.app');self.target.rename(old);self.target.mkdir()
        with patch.object(helper_entry,'InstallTransaction',return_value=self.tx), \
             patch.object(helper_entry,'process_identity',return_value=None), \
             patch.object(helper_entry.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'apfs','')), \
             patch.object(helper_entry.subprocess,'Popen') as reopen:
            helper_entry._reopen_untouched_app(path)
        reopen.assert_not_called();self.assertTrue(self.tx.owns(plan['txn']))

    def test_disk_errno_is_saved_without_exception_message(self):
        import json
        from src.core.diagnostics import record_failure
        error=OSError(28,'private arbitrary content')
        record_failure('update_helper',error,root=self.root/'diagnostic')
        raw=(self.root/'diagnostic/diagnostics/failures.json').read_text()
        self.assertNotIn('private arbitrary content',raw)
        self.assertEqual(json.loads(raw)['events'][-1]['causes'][0]['errno'],28)
