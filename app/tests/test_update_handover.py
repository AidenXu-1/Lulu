"""Only temporary files, fake signed-process replies and memory credentials."""
import json
import hashlib
import base64
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from src.core.updater import handover as h, helper_entry
from src.core.updater.models import UpdateError, UpdateSnapshot


class HandoverLockTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()/'updates'
        self.patch = patch.object(h, 'default_updates_root', return_value=self.root)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def test_export_allows_only_closed_reads_and_freeze_blocks_them_too(self):
        read = {'command':'argv','arguments':{'argv':['jobs','list','--output','json']}}
        write = {'command':'gui.jobs.create','arguments':{'task':{}}}
        with h.handover_lock(self.root, exclusive=True) as fd:
            h._phase(fd, b'export\n')
            with h.business_request_guard(read): pass
            with self.assertRaises(UpdateError):
                with h.business_request_guard(write): pass
            h._phase(fd, b'frozen\n')
            with self.assertRaises(UpdateError):
                with h.business_request_guard(read): pass
        with h.business_request_guard(write): pass

    def test_inflight_request_prevents_freeze_instead_of_cancelling_it(self):
        with h.business_request_guard({'command':'gui.jobs.create','arguments':{}}):
            with self.assertRaisesRegex(UpdateError, '正在保存'):
                with h.handover_lock(self.root, exclusive=True): pass
        with h.handover_lock(self.root, exclusive=True): pass

    def test_symlink_lock_is_rejected_without_following_target(self):
        directory=self.root/'transactions';directory.mkdir(parents=True)
        victim=self.root/'user-file';victim.write_text('keep')
        (directory/'handover.lock').symlink_to(victim)
        with self.assertRaises(OSError):
            with h.handover_lock(self.root): pass
        self.assertEqual(victim.read_text(),'keep')

    def test_production_dispatcher_cannot_construct_runtime_during_handover(self):
        from src.core.agent_entry.entrypoints import CoreCommandDispatcher
        factory=Mock(side_effect=AssertionError('must not start a job or runtime'))
        dispatcher=CoreCommandDispatcher(product_identity={},
            resource_root=self.root/'Lulu.app/Contents/Resources',runtime_factory=factory)
        with h.handover_lock(self.root,exclusive=True) as fd:
            h._phase(fd,b'export\n')
            for request in (
                {'command':'gui.jobs.create','arguments':{'task':{}}},
                {'command':'argv','arguments':{'argv':['transcribe','start','--output','json']}},
                {'command':'argv','arguments':{'argv':['jobs','list','--output','json','--extra']}},
            ):
                with self.assertRaises(UpdateError):dispatcher(request)
        factory.assert_not_called()


class PreSwapTests(TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.product=Path(self.temp.name).resolve();self.root=self.product/'updates'
        self.path=self.root/'transactions'/('a'*64)/'install_plan.json'
        self.path.parent.mkdir(parents=True)
        self.state=self.product/'AgentEntry/app-generations'/('b'*64)
        self.state.mkdir(parents=True)
        self.app=self.product/'Applications/Lulu.app'
        self.plan={'target_app':str(self.app),'old_pid':1,'txn':'a'*64,'nonce':'b'*64,'plan_sha256':'c'*64,'ready_path':str(self.path.parent/'ready.json')}
        self.plan.update(staged_app=str(self.product), target_parent=str(self.product))
        self.service=Mock();self.service.is_loaded.return_value=False
        self.patches=[patch('src.core.agent_entry.standalone_paths.state_for_app',return_value=self.state),
            patch('src.core.agent_entry.standalone_paths.service_for_app',return_value='fixture'),
            patch('src.core.updater.restart_recovery.running_app',return_value=False),
            patch.object(h,'_assert_old_core_stopped'),
            patch('src.core.agent_entry.effects.CurrentUserLaunchctlService',return_value=self.service)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        cp=self.product/'AgentEntry/history-checkpoints'/('c'*64+'.json')
        cp.parent.mkdir();cp.write_bytes(b'authenticated-by-child')
        self.proof={'schema':1,'manifest':'c'*64,'generation':'d'*40,'state':str(self.state),
            'checkpoint_sha256':hashlib.sha256(cp.read_bytes()).hexdigest(),
            'snapshot':'e'*64,'handover_snapshot':'f'*64}

    def test_signed_export_precedes_stop_and_recheck(self):
        events=[]
        self.service.terminate_registered_process.side_effect=lambda:events.append('terminate')
        self.service.stop.side_effect=lambda:events.append('stop')
        def export(*args,**kwargs):
            events.append('export')
            self.assertEqual(args[0][1:],[ '--prepare-update-checkpoint',str(self.path)])
            return SimpleNamespace(returncode=0,stdout=json.dumps(self.proof).encode())
        def snapshot(path):
            events.append('recheck')
            return 'e'*64 if path.name=='d'*40 else 'f'*64
        item=h.PreSwapHandover(self.path,self.plan)
        try:
            with patch.object(h.subprocess,'run',side_effect=export),patch('src.core.agent_entry.product_transaction.snapshot_digest',side_effect=snapshot):
                item.prepare()
            self.assertEqual(events,['export','terminate','stop','recheck','recheck'])
            self.assertEqual((self.root/'transactions/handover.lock').read_bytes(),b'frozen\n')
        finally:item.release()

    def test_unfinished_or_failed_export_never_stops_core(self):
        item=h.PreSwapHandover(self.path,self.plan)
        with patch.object(h.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout=b'')):
            with self.assertRaises(UpdateError):item.prepare()
        item.restore()
        self.service.terminate_registered_process.assert_not_called()
        self.service.stop.assert_not_called();self.service.start.assert_not_called()
        with h.handover_lock(self.root,exclusive=True):pass

    def test_changed_tasks_abort_and_restore_registered_old_core(self):
        item=h.PreSwapHandover(self.path,self.plan)
        with patch.object(h.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(self.proof).encode())),patch('src.core.agent_entry.product_transaction.snapshot_digest',return_value='0'*64):
            with self.assertRaisesRegex(UpdateError,'发生变化'):item.prepare()
        item.restore()
        self.service.start.assert_called_once_with(self.state/'core.plist')

    def test_helper_preflight_failure_never_enters_swap_recovery(self):
        tx=Mock();tx.root=self.root;tx.load_plan.return_value=self.plan
        store=Mock();store.load.return_value=UpdateSnapshot()
        item=Mock();item.prepare.side_effect=UpdateError('handover_failed','fixture')
        def save(_snapshot):
            tx.release.assert_not_called()
        store.save.side_effect=save
        with patch.object(helper_entry,'InstallTransaction',return_value=tx),patch('src.core.updater.helper_protocol.StateStore',return_value=store),patch.object(helper_entry,'process_identity',return_value=None),patch.object(helper_entry,'PreSwapHandover',return_value=item),patch.object(helper_entry.subprocess,'Popen') as launch:
            self.assertEqual(helper_entry._run_helper(str(self.path)),5)
        tx.execute_local.assert_not_called();tx.recover.assert_not_called()
        item.restore.assert_called_once();tx.release.assert_called_once()
        self.assertEqual(store.save.call_args.args[0].error_code,'handover_failed')
        self.assertEqual(launch.call_args.args[0],[str(self.app/'Contents/MacOS/Lulu')])

    def test_failed_result_write_preserves_transaction_lock_without_swapping(self):
        tx=Mock();tx.root=self.root;tx.load_plan.return_value=self.plan
        store=Mock();store.load.return_value=UpdateSnapshot();store.save.side_effect=OSError('disk full')
        item=Mock();item.prepare.side_effect=UpdateError('handover_failed','fixture')
        with patch.object(helper_entry,'InstallTransaction',return_value=tx),patch('src.core.updater.helper_protocol.StateStore',return_value=store),patch.object(helper_entry,'process_identity',return_value=None),patch.object(helper_entry,'PreSwapHandover',return_value=item),patch('src.core.diagnostics.record_failure') as diagnostic,patch.object(helper_entry.subprocess,'Popen') as launch:
            self.assertEqual(helper_entry._run_helper(str(self.path)),5)
        tx.execute_local.assert_not_called();tx.recover.assert_not_called()
        tx.release.assert_not_called();item.release.assert_called_once()
        launch.assert_called_once();diagnostic.assert_called_once()

    def test_guard_explicit_fixture_root_never_falls_back_to_user_home(self):
        with patch.object(h,'default_updates_root',side_effect=AssertionError('unexpected real root')):
            with h.business_request_guard({'command':'gui.jobs.create','arguments':{}},root=self.root):pass

    def test_process_query_failure_after_export_restores_old_service_before_unlock(self):
        tx=Mock();tx.root=self.root;tx.load_plan.return_value=self.plan
        tx.execute_local.side_effect=UpdateError('process_check','fixture')
        store=Mock();store.load.return_value=UpdateSnapshot()
        item=Mock()
        for restore_error,code in [(None,'handover_process_check'),
                (RuntimeError('restore denied'),'handover_restore_failed')]:
            with self.subTest(code=code):
                item.reset_mock();tx.reset_mock();store.reset_mock()
                item.restore.side_effect=restore_error
                def save(snapshot):
                    tx.release.assert_not_called()
                    item.restore.assert_called_once()
                    self.assertEqual(snapshot.error_code,code)
                    self.assertEqual(snapshot.ui_state,'preparation_error')
                store.save.side_effect=save
                with patch.object(helper_entry,'InstallTransaction',return_value=tx),patch('src.core.updater.helper_protocol.StateStore',return_value=store),patch.object(helper_entry,'process_identity',return_value=None),patch.object(helper_entry,'PreSwapHandover',return_value=item),patch.object(helper_entry.subprocess,'Popen') as launch,patch.object(helper_entry,'stop_update_core') as stop:
                    self.assertEqual(helper_entry._run_helper(str(self.path)),5)
                tx.recover.assert_not_called();stop.assert_not_called()
                tx.release.assert_called_once();launch.assert_called_once()

    def test_invalid_export_proof_never_stops_old_core(self):
        for field,value in [('schema',True),('state',str(self.product/'other')),
                ('manifest','../escape'),('generation','../escape'),('handover_snapshot',[])]:
            with self.subTest(field=field):
                proof=dict(self.proof);proof[field]=value
                item=h.PreSwapHandover(self.path,self.plan)
                try:
                    with patch.object(h.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(proof).encode())):
                        with self.assertRaises(UpdateError):item.prepare()
                finally:item.restore()
                self.service.terminate_registered_process.assert_not_called()


class CheckpointEntryTests(TestCase):
    def test_signed_entry_requires_freeze_and_reauthenticates_checkpoint(self):
        from src.core.agent_entry.history_checkpoint import write_checkpoint
        from src.core.agent_entry.product_transaction import snapshot_digest
        with TemporaryDirectory() as temp, ExitStack() as stack:
            product=Path(temp).resolve();root=product/'updates'
            plan_path=root/'transactions'/('a'*64)/'install_plan.json'
            plan_path.parent.mkdir(parents=True)
            lock=root/'transactions/update.lock';lock.write_text('a'*64);lock.chmod(0o600)
            state=product/'AgentEntry';state.mkdir()
            generation='b'*40
            taskroot=product/'task-generations'/generation;taskroot.mkdir(parents=True)
            (taskroot/'ledger').write_text('authenticated original')
            owner={'installation_id':'fixture','installation_id_digest':hashlib.sha256(b'fixture').hexdigest(),
                'component_manifest_sha256':'c'*64}
            app=product/'Lulu.app';keys=Mock()
            keys.get_text.return_value=base64.b64encode(b'x'*32).decode()
            prepare=Mock(side_effect=lambda *_a,**_k:write_checkpoint(product_root=product,
                owner=owner,generation=generation,keys=keys,records=[],context={'tasks':{},
                'view':{'groups':[],'hidden_ids':[]}},expected_snapshot=snapshot_digest(taskroot),state=state))
            stack.enter_context(patch.object(h,'default_updates_root',return_value=root))
            stack.enter_context(patch('threading.Thread'))
            stack.enter_context(patch('src.core.agent_entry.standalone_paths.frozen_app',return_value=app))
            stack.enter_context(patch('src.core.agent_entry.standalone_paths.state_for_app',return_value=state))
            stack.enter_context(patch('src.core.updater.install.InstallTransaction.load_plan',return_value={'target_app':str(app),'old_pid':1,'txn':'a'*64}))
            stack.enter_context(patch('src.core.updater.process_identity.process_identity',return_value=None))
            key_factory=stack.enter_context(patch('src.core.cli_runtime.core_task_keychain.CoreTaskKeyringStore',return_value=keys))
            stack.enter_context(patch('src.core.agent_entry.ownership.verify_ownership_receipt',return_value=owner))
            stack.enter_context(patch('src.core.agent_entry.standalone_source.core_cdhash',return_value=generation))
            stack.enter_context(patch('src.core.agent_entry.history_checkpoint.prepare_checkpoint',prepare))
            stack.enter_context(patch('src.core.diagnostics.record_failure'))
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(h.checkpoint_entry([str(plan_path)]),1)
            key_factory.assert_not_called();prepare.assert_not_called();self.assertEqual(output.getvalue(),'')
            with h.handover_lock(root,exclusive=True) as fd:
                h._phase(fd,b'export\n')
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(h.checkpoint_entry([str(plan_path)]),0)
                proof=json.loads(output.getvalue())
                self.assertEqual(proof['generation'],generation)
                self.assertEqual(set(proof),{'schema','manifest','generation','state','checkpoint_sha256','snapshot','handover_snapshot'})
                checkpoint=product/'AgentEntry/history-checkpoints'/('c'*64+'.json')
                value=json.loads(checkpoint.read_bytes());value['hmac_sha256']='0'*64
                checkpoint.write_text(json.dumps(value))
                prepare.side_effect=None;prepare.return_value=checkpoint
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(h.checkpoint_entry([str(plan_path)]),1)
                self.assertEqual(output.getvalue(),'')

    def test_process_scan_requires_full_identity_and_observed_exit(self):
        app=Path('/fixture/long name/Lulu.app')
        for response in (SimpleNamespace(returncode=1,stdout=''),
                SimpleNamespace(returncode=0,stdout='bad line'),
                SimpleNamespace(returncode=0,stdout='77 '+str(app/'Contents/MacOS/lulu-agent-core'))):
            with patch.object(h.subprocess,'run',return_value=response):
                with self.assertRaises(UpdateError):h._assert_old_core_stopped(app)
        with patch.object(h.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='78 /fixture/other-core')) as ps:
            h._assert_old_core_stopped(app)
        self.assertEqual(ps.call_args.args[0],['/bin/ps','-ww','-axo','pid=,comm='])


class InheritedExportTests(TestCase):
    def test_authenticated_inherited_rows_need_one_public_list_only(self):
        from src.core.agent_entry.task_handover import capture_terminal_tasks
        rows=[{'job_id':'old-'+str(i),'job_kind':'link_extraction','job_revision':1,'state':'failed',
              'lease_epoch':0,'last_sequence':1,'result_ref':None,
              'receipt_ref':'lulu-job:old-'+str(i)+':revision:1','partial_failures':[]} for i in range(245)]
        cli=Mock(return_value={'jobs':[{k:v for k,v in r.items() if k!='partial_failures'} for r in rows]})
        actual=capture_terminal_tasks(cli,inherited=rows)
        self.assertEqual(len(actual),245);cli.assert_called_once_with(['jobs','list','--output','json'])
        cli.return_value['jobs'][0]['job_revision']=2
        with self.assertRaisesRegex(RuntimeError,'PRODUCT_TASKS_CHANGED'):capture_terminal_tasks(cli,inherited=rows)
