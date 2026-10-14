"""Review regressions: fake API/browser/credentials, temporary signed App and ledger."""
import io
import json
import time
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QWidget
from src.core import feishu, secrets, feishu_save as saving
from src.core.feishu_storage import WorkArchiveRecord
from src.ui.feishu_connection import FeishuConnectionPanel
from src.ui.feishu_save_dialog import FeishuSaveSession
from src.ui.main_window import MainWindow

APP = QApplication.instance() or QApplication([])

def settle(obj):
    deadline = time.monotonic() + 3
    while obj.busy and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(.005)
    APP.processEvents()
    assert not obj.busy


class MemoryBackend:
    def __init__(self): self.data = {}
    def get_password(self, service, name): return self.data.get((service, name))
    def set_password(self, service, name, value): self.data[service, name] = value
    def delete_password(self, service, name): self.data.pop((service, name), None)


class Host(QWidget):
    open_feishu_connection = MainWindow.open_feishu_connection
    _registered_qthreads = MainWindow._registered_qthreads
    _settle_registered_qthreads = MainWindow._settle_registered_qthreads
    has_active_work = MainWindow.has_active_work
    def _switch_page(self, index): self.index = index


class UIProbes(unittest.TestCase):
    def setUp(self):
        self.stack = __import__('contextlib').ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(secrets, '_backend', return_value=MemoryBackend()))
        self.stack.enter_context(patch.dict(secrets._cache, {}, clear=True))
        self.stack.enter_context(patch.dict(secrets._memory, {}, clear=True))
        self.stack.enter_context(patch.dict(feishu._user_cache, {'token': '', 'exp': 0}, clear=True))
        self.stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('NETWORK FORBIDDEN')))
        self.stack.enter_context(patch('webbrowser.open', return_value=True))
        self.cfg = SimpleNamespace(feishu_last_save={}, save=lambda: True)
        self.host = Host()
        self.host.tasks = []
        self.host._mixed_rec_active = self.host._mixed_pending_stop = False
        self.host._system_rec_session = None
        self.panel = FeishuConnectionPanel(self.cfg, self.host)
        self.host.settings_page = SimpleNamespace(feishu_connection=self.panel, set_category=lambda _: None)
        self.sessions = []
        self.addCleanup(self.cleanup_ui)

    def cleanup_ui(self):
        settle(self.panel)
        for session in self.sessions:
            settle(session.dialog)
            session.dialog.reject()
        self.panel.shutdown()
        self.host.close()
        self.host.deleteLater()
        APP.processEvents()

    def session(self):
        session = FeishuSaveSession(self.host, self.cfg,
            (WorkArchiveRecord(title='Synthetic manuscript', transcript_text='Synthetic body'),),
            'Synthetic manuscript', 'single')
        self.sessions.append(session)
        session.start(); settle(session.dialog)
        return session

    def test_changed_app_rejects_old_token_and_disables_save(self):
        secrets.set_config(feishu.CONFIG_KEY, {'app_id':'cli_old', 'app_secret':'fake_old', 'refresh_token':'fake_refresh'})
        feishu._store({'access_token':'fake_old_access','expires_in':3600})
        used = []
        def api(method, path, token, **kw):
            used.append((method, path, token))
            self.assertEqual(path, '/authen/v1/user_info')
            return {'open_id':'old_account', 'tenant_key':'old_tenant', 'name':'OLD ACCOUNT'}
        with patch.object(feishu, '_api', side_effect=api):
            d = self.session().dialog
            old_account = d.account['account_id']
            d.url.setText('https://old.feishu.cn/base/OLDTARGET')
            d.connect_button.click()
            self.panel.loaded_config(secrets.get_config(feishu.CONFIG_KEY))
            self.panel.app_id.setText('cli_new'); self.panel.app_id.setModified(True)
            self.panel.secret.setText('fake_new'); self.panel.secret.setModified(True)
            # Model cancellation before any token exchange: authorize receives saved new credentials.
            with patch.object(self.panel, 'authorize', side_effect=self.panel.loaded_config):
                self.panel.start_login(); settle(self.panel)
            self.assertFalse(self.panel.authorized)
            self.panel.return_button.click(); settle(d)
            self.assertFalse(d.save_button.isEnabled())
            self.assertEqual(d.account, {})
            self.assertFalse(feishu._user_cache['token'])

    def test_oauth_cancel_during_exchange_does_not_commit_token(self):
        secrets.set_config(feishu.CONFIG_KEY, {'app_id':'cli_test', 'app_secret':'fake_secret'})
        entered, release = Event(), Event()
        state = {}
        class Server:
            def __init__(self, address, handler): self.handler=handler
            def handle_request(server):
                request=SimpleNamespace(path='/callback?code=fake_code&state='+state['value'],
                    send_response=lambda _:None, send_header=lambda *a:None, end_headers=lambda:None, wfile=io.BytesIO())
                server.handler.do_GET(request)
            def server_close(self): pass
        def url(value, redirect): state['value']=value; return 'https://example.invalid/fake'
        def exchange(body, timeout=20):
            entered.set(); release.wait(3)
            return {'access_token':'fake_after_cancel', 'refresh_token':'fake_after_cancel_refresh', 'expires_in':3600}
        with patch('http.server.HTTPServer', Server), patch.object(feishu, 'authorize_url', side_effect=url), patch.object(feishu, '_oauth_token', side_effect=exchange):
            self.panel.authorize(secrets.get_config(feishu.CONFIG_KEY))
            try:
                self.assertTrue(entered.wait(2))
                self.panel.login.click()
                self.assertIn('停止', self.panel.notice.text())
            finally: release.set()
            settle(self.panel)
        self.assertFalse(feishu.is_authorized())
        self.assertFalse(feishu._user_cache['token'])

    def test_account_switch_keeps_writes_bound_to_confirmed_account(self):
        secrets.set_config(feishu.CONFIG_KEY, {'app_id':'cli_test','app_secret':'fake_secret','refresh_token':'fake_A_refresh'})
        feishu._store({'access_token':'fake_A_access','expires_in':3600})
        identity={'open_id':'account_A', 'tenant_key':'tenant_A', 'name':'A'}
        account=saving.digest([identity['open_id'],identity['tenant_key']])
        writes=[]
        def api(method, path, token, **kwargs):
            if path=='/authen/v1/user_info':
                self.assertEqual(token, 'fake_A_access')
                # OAuth completes while the old account-info HTTP response is in flight.
                feishu._store({'access_token':'fake_B_access','refresh_token':'fake_B_refresh','expires_in':3600})
                return identity
            if method=='POST': writes.append({'path':path, 'token':token})
            if path=='/docx/v1/documents': return {'document':{'document_id':'B_DOC','revision_id':1}}
            if path.endswith('/children'): return {'document_revision_id':2}
            if path.endswith('/blocks'): return {'items':[], 'has_more':False}
            if path=='/docx/v1/documents/B_DOC': return {'document':{'revision_id':2}}
            raise AssertionError(path)
        values={}
        store=SimpleNamespace(read=lambda k:dict(values.get(k,{})),write=lambda k,v:values.__setitem__(k,dict(v)))
        selection={'account_id':account,'format':'document','operation_id':'synthetic-op','destination_kind':'drive','single_document':True,'name':'Synthetic manuscript'}
        with patch.object(feishu,'_api',side_effect=api):
            result=saving.save_records((WorkArchiveRecord(title='Synthetic manuscript',transcript_text='Synthetic body'),), selection, store=store)
        self.assertEqual(result.row_success,1)
        self.assertEqual(len(writes),2)
        self.assertTrue(all(w['token']=='fake_A_access' for w in writes))
        self.assertEqual(feishu._user_token(),'fake_B_access')
        with patch.object(feishu,'_api',return_value={'open_id':'account_B','tenant_key':'tenant_B'}):
            with self.assertRaisesRegex(ValueError,'账号发生变化'):
                saving.save_records((),selection,store=store)

    def test_connection_worker_participates_in_exit_gates(self):
        entered, release = Event(), Event()
        def read(): entered.set(); release.wait(3); return {}
        self.panel.run_call(read, self.panel.loaded_config)
        try:
            self.assertTrue(entered.wait(1))
            worker = self.panel.worker
            self.assertTrue(worker.isRunning())
            self.assertTrue(self.host.has_active_work())
            self.assertIn(worker, self.host._registered_qthreads())
            self.assertFalse(self.host._settle_registered_qthreads(1))
            self.panel.shutdown()
            self.assertTrue(worker.isRunning())

        finally: release.set(); settle(self.panel)

    def test_main_close_reaches_cleanup_while_passive_library_read_is_blocked(self):
        from PySide6.QtGui import QCloseEvent
        from src.ui.collection_page import CollectionPage
        from src.core import library
        entered, release = Event(), Event()
        def read(): entered.set(); release.wait(3); return []
        page=CollectionPage(); self.host.collection_page=page
        reached=[]
        self.host.cfg=self.cfg; self.cfg.confirm_exit_during_tasks=False
        self.host.library_page=self.host.models_page=None
        self.host.update_controller=None
        self.host._choose_xhs_exit_action=lambda:'none'
        self.host._settle_xhs_exit=lambda _: reached.append('after_workers') or False
        self.host._defer_close_until_workers_settle=lambda event:event.ignore()
        reader=None
        try:
            with patch.object(library,'list_entries',side_effect=read):
                page.refresh(); self.assertTrue(entered.wait(1)); reader=page._transcribed_read
                started=time.monotonic()
                MainWindow.closeEvent(self.host,QCloseEvent())
                self.assertEqual(reached,['after_workers'])
                self.assertLess(time.monotonic()-started,.4)
                self.assertTrue(reader.isRunning())
        finally:
            release.set()
            if reader:reader.wait(2000)
            page.shutdown_workers();page.deleteLater();self.host.collection_page=None



class UpgradeProbe(unittest.TestCase):
    def test_retry_prepared_candidate_recaptures_history_after_rollback(self):
        from test_standalone_install_source import StandaloneInstallSourceTests
        from test_generation9_formal_install_public import MemoryKeys
        from test_core_storage_binding import CoreMemoryKeys
        from test_phase5_job_ledger import _create_arguments
        from src.core.agent_entry.standalone_state import prepare_install_state
        from src.core.agent_entry.standalone_source import DESCRIPTOR
        from src.core.agent_entry.installed_materials import MATERIALS_DIR, _private_json
        from src.core.agent_entry.task_handover import FILENAME, open_handover
        from src.core.agent_entry.standalone_history import verified_terminal_rows
        from src.core.job_ledger.storage_binding import prepare_core_task_store
        from src.core.job_ledger.contracts import JobState
        import subprocess
        StandaloneInstallSourceTests.setUp(self)
        StandaloneInstallSourceTests.prepare(self)
        product=self.root/'product'; product.mkdir()
        keys=MemoryKeys()
        old=prepare_install_state(self.app, product_root=product, keys=keys)
        old_owner=_private_json(old/'ownership.json')
        ledger_keys=CoreMemoryKeys()
        ledger_root=product/'task-generations'/'old-synthetic-generation'
        ledger_root.mkdir(parents=True)
        ledger=prepare_core_task_store(ledger_root, old_owner['installation_id'], ledger_keys, legacy_storage=False)
        def capture(*args):
            return verified_terminal_rows(ledger_root, old_owner['installation_id'], ledger_keys), None
        # Distinct signed candidate identity, same micro-fixture binary; no installed App involved.
        descriptor=self.app/DESCRIPTOR
        value=json.loads(descriptor.read_text()); value['capability_closed_set'].append('zz-review-candidate')
        descriptor.write_text(json.dumps(value))
        subprocess.run(['/usr/bin/codesign','--force','--deep','--sign','-',str(self.app)], check=True, capture_output=True)
        from unittest.mock import Mock
        loader=Mock(side_effect=capture)
        candidate=prepare_install_state(self.app, product_root=product, keys=keys, previous_state=old, handover_loader=loader)
        # After helper rollback, old generation accepts one further completed task.
        ledger.create_job(**dict(_create_arguments(), installation_id=old_owner['installation_id']))
        ledger.transition('job-001', expected_revision=1, new_state=JobState.QUEUED, changed_at='2026-08-20T01:00:01Z')
        ledger.claim('job-001', expected_revision=2, worker_instance_id='review', operation_receipt=None, changed_at='2026-08-20T01:00:02Z')
        ledger.transition('job-001', expected_revision=3, new_state=JobState.SUCCEEDED, changed_at='2026-08-20T01:00:03Z', result_ref='lulu-result:job-001:sha256:'+'a'*64)
        self.assertEqual(len(capture()[0]), 1)
        again=prepare_install_state(self.app, product_root=product, keys=keys, previous_state=old, handover_loader=loader)
        owner=_private_json(again/'ownership.json')
        history=open_handover(_private_json(again/MATERIALS_DIR/FILENAME), keys=keys, owner=owner, product_root=product)
        self.assertEqual(again, candidate); self.assertEqual(loader.call_count, 2); self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['job_id'], 'job-001')


class PassiveReadProcessTests(unittest.TestCase):
    def test_process_exit_does_not_wait_for_blocked_read_or_destroy_running_qthread(self):
        import subprocess,sys,tempfile
        code='''
import sys, threading
from pathlib import Path
from unittest.mock import patch
with patch.object(Path, 'home', return_value=Path(sys.argv[1])):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from src.ui.collection_page import _TranscribedRead
    app=QApplication([])
    entered=threading.Event(); release=threading.Event()
    def read():
        entered.set(); release.wait(30); return set()
    reader=_TranscribedRead(read); reader.start()
    assert entered.wait(2)
    QTimer.singleShot(10, app.quit)
    app.exec()
    assert reader.isRunning()
'''
        with tempfile.TemporaryDirectory() as home:
            result=subprocess.run([sys.executable,'-B','-c',code,home],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('QThread: Destroyed',result.stderr)


class UpgradeRuntimeTests(unittest.TestCase):
    def setUp(self):
        from test_standalone_install_source import StandaloneInstallSourceTests
        from test_generation9_formal_install_public import MemoryKeys
        from test_core_storage_binding import CoreMemoryKeys
        from src.core.agent_entry.standalone_state import prepare_install_state
        from src.core.agent_entry.installed_materials import _private_json
        from src.core.job_ledger.storage_binding import prepare_core_task_store
        import shutil
        StandaloneInstallSourceTests.setUp(self); StandaloneInstallSourceTests.prepare(self)
        self.home=self.root/'home'; self.product=self.home/'Library/Application Support/Lulu'
        self.product.mkdir(parents=True); self.keys=MemoryKeys()
        self.a=prepare_install_state(self.app,product_root=self.product,keys=self.keys)
        self.owner=_private_json(self.a/'ownership.json')
        self.backup=self.root/'backup/Lulu.app';self.backup.parent.mkdir()
        shutil.copytree(self.app,self.backup,symlinks=True)
        self.old_keys=CoreMemoryKeys()
        (self.product/'task-generations'/self.owner['core_cdhash']).mkdir(parents=True,mode=0o700)
        self.ledger=prepare_core_task_store(self.product/'task-generations'/self.owner['core_cdhash'],
            self.owner['installation_id'],self.old_keys,legacy_storage=False)
        self.stack=__import__('contextlib').ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(Path,'home',return_value=self.home))
        self.stack.enter_context(patch('src.core.agent_entry.standalone_runtime.frozen_app',return_value=self.app))
        self.stack.enter_context(patch('src.core.cli_runtime.core_task_keychain.CoreTaskKeyringStore',return_value=self.keys))
        self.stack.enter_context(patch('src.core.agent_entry.standalone_history.historical_keys',return_value=self.old_keys))
        self.stack.enter_context(patch('src.core.agent_entry.effects.CurrentUserLaunchctlService'))

    def candidate(self, tag, executable):
        import shutil,subprocess
        from src.core.agent_entry.standalone_source import DESCRIPTOR
        value=json.loads((self.app/DESCRIPTOR).read_text());value['capability_closed_set'].append(tag)
        (self.app/DESCRIPTOR).write_text(json.dumps(value))
        shutil.copyfile(executable,self.app/'Contents/MacOS/lulu-agent-core')
        subprocess.run(['/usr/bin/codesign','--force','--deep','--sign','-',str(self.app)],check=True,capture_output=True)

    def task(self, *, terminal=True):
        from test_phase5_job_ledger import _create_arguments
        from src.core.job_ledger.contracts import JobState
        self.ledger.create_job(**dict(_create_arguments(),installation_id=self.owner['installation_id']))
        if terminal:
            self.ledger.transition('job-001',expected_revision=1,new_state=JobState.QUEUED,changed_at='2026-08-20T01:00:01Z')
            self.ledger.claim('job-001',expected_revision=2,worker_instance_id='review',operation_receipt=None,changed_at='2026-08-20T01:00:02Z')
            self.ledger.transition('job-001',expected_revision=3,new_state=JobState.SUCCEEDED,changed_at='2026-08-20T01:00:03Z',result_ref='lulu-result:job-001:sha256:'+'a'*64)

    def history(self, state):
        from src.core.agent_entry.task_handover import FILENAME,open_handover
        from src.core.agent_entry.installed_materials import MATERIALS_DIR,_private_json
        path=state/MATERIALS_DIR/FILENAME
        return open_handover(_private_json(path),keys=self.keys,owner=_private_json(state/'ownership.json'),product_root=self.product)

    def test_runtime_retry_and_later_candidate_include_restored_old_tasks(self):
        from src.core.agent_entry.standalone_runtime import prepare_current_app,find_previous_state
        from src.core.agent_entry.standalone_state import install_state_root
        self.candidate('zz-b','/usr/bin/false')
        b=prepare_current_app(backup_app=self.backup)
        self.task()
        # Ordinary launch covers drag replacement with no update-plan argument.
        self.assertEqual(prepare_current_app(),b)
        self.assertEqual([r['job_id'] for r in self.history(b)],['job-001'])
        self.candidate('zz-c','/usr/bin/printf')
        self.assertEqual(find_previous_state(self.product,target=install_state_root(self.app,self.product),keys=self.keys,backup_app=self.backup),self.a)
        c=prepare_current_app(backup_app=self.backup)
        self.assertEqual([r['job_id'] for r in self.history(c)],['job-001'])

    def test_unfinished_predecessor_blocks_retry_and_preserves_sealed_history(self):
        from src.core.agent_entry.standalone_runtime import prepare_current_app
        from src.core.agent_entry.product_transaction import snapshot_digest
        self.candidate('zz-b','/usr/bin/false')
        b=prepare_current_app(backup_app=self.backup);self.task(terminal=False)
        before=snapshot_digest(b)
        with self.assertRaisesRegex(RuntimeError,'PRODUCT_UNFINISHED_TASKS'):
            prepare_current_app(backup_app=self.backup)
        self.assertEqual(snapshot_digest(b),before)

    def test_unchanged_history_is_not_rewritten_or_bundle_rescanned(self):
        from src.core.agent_entry.standalone_runtime import prepare_current_app
        from src.core.agent_entry.installed_materials import MATERIALS_DIR
        from src.core.agent_entry.task_handover import FILENAME
        self.candidate('zz-b','/usr/bin/false')
        b=prepare_current_app(backup_app=self.backup);path=b/MATERIALS_DIR/FILENAME
        before=(path.stat().st_ino,path.stat().st_mtime_ns,path.read_bytes())
        with patch('src.core.agent_entry.standalone_state.verify_component_manifest',side_effect=AssertionError('unnecessary full scan')):
            prepare_current_app()
        self.assertEqual((path.stat().st_ino,path.stat().st_mtime_ns,path.read_bytes()),before)

    def test_history_identity_evidence_is_authenticated(self):
        from src.core.agent_entry.standalone_runtime import prepare_current_app
        from src.core.agent_entry.installed_materials import MATERIALS_DIR
        from src.core.agent_entry.task_handover import FILENAME
        self.candidate('zz-b','/usr/bin/false')
        b=prepare_current_app(backup_app=self.backup);path=b/MATERIALS_DIR/FILENAME
        value=json.loads(path.read_text());value['body']['source_cores']={'a'*64:'b'*40}
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError,'PRODUCT_TASK_HANDOVER_INVALID'):
            prepare_current_app()

    def test_legacy_identity_uses_verified_backup_then_authenticated_evidence(self):
        from src.core.agent_entry.standalone_history import previous_core_identity
        # Identity resolver is called only after ownership authentication by its caller.
        legacy=dict(self.owner,schema_id='lulu.agent-entry-ownership/v3')
        legacy.pop('core_cdhash');legacy.pop('app_path')
        with self.assertRaisesRegex(RuntimeError,'PRODUCT_PREVIOUS_CORE_REQUIRED'):
            previous_core_identity(self.a,legacy)
        evidence={}
        with_backup=previous_core_identity(self.a,legacy,backup_app=self.backup,known_cores=evidence)
        self.assertEqual(previous_core_identity(self.a,legacy,known_cores=evidence),with_backup)
        self.assertEqual(len(evidence),1)
