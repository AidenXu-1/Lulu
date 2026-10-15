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
