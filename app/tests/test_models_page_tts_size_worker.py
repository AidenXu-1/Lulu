"""V2 state reads and installs remain owned until the actual thread exits."""
import threading
from PySide6.QtCore import QThread
from tests.model_settings_fixtures import ControllerCase, m

class ModelsPageOwnedWorkerTests(ControllerCase):
    def test_existing_tts_with_missing_environment_offers_repair(self):
        with self.public_state(status='runtime_only', is_installed=True):
            state = self.c._read_state()
        for model in state['localModels']['voice']:
            self.assertTrue(model['repair'])
            self.assertFalse(model['ready'])
        for feature in state['voiceFeatures']:
            self.assertTrue(feature['installed'])
            self.assertFalse(feature['ready'])

    def test_shutdown_keeps_blocking_worker_until_natural_finish(self):
        release=threading.Event();started=threading.Event()
        class BlockingWorker(QThread):
            def request_cancel(self):self.requestInterruption()
            def run(self):started.set();release.wait(3)
        worker=BlockingWorker();self.c._start(worker,key='fixture',mid='local_voxcpm2')
        try:
            self.assertTrue(started.wait(1))
            self.assertFalse(self.c.shutdown_workers(1))
            self.assertTrue(worker.isInterruptionRequested())
            self.assertIn(worker,self.c.owned_workers())
            self.assertTrue(self.c.has_active_work())
        finally:
            release.set();self.assertTrue(worker.wait(1000));self.app.processEvents()
        self.assertTrue(self.c.shutdown_workers(0))
        self.assertEqual(self.c.owned_workers(),[])

    def test_closing_read_suppresses_late_snapshot_and_followup(self):
        release=threading.Event();started=threading.Event();values=[]
        def read():started.set();release.wait(3);return {'sentinel':True}
        self.c._read_state=read;self.c.changed.connect(values.append)
        m.ModelController.refresh(self.c)
        try:
            self.assertTrue(started.wait(1));m.ModelController.refresh(self.c)
            self.assertFalse(self.c.shutdown_workers(1))
        finally:
            release.set();self.wait_for(lambda:not self.c.owned_workers())
        self.assertEqual(values,[])
        self.c.refresh.assert_not_called()

    def test_public_snapshot_keeps_repair_and_resume_independent(self):
        with self.public_state(status='needs_repair',partial_install_bytes=321):
            state=self.c._read_state()
        model=next(x for x in state['localModels']['voice'] if x['id']=='voxcpm2')
        self.assertTrue(model['repair']);self.assertFalse(model['ready']);self.assertEqual(model['resumable'],321)

    def test_relocation_rechecks_new_model_work_after_dialog(self):
        from pathlib import Path
        from unittest.mock import patch
        busy=[False]
        self.c.model_in_use=lambda mid:'new inference' if busy[0] else ''
        def confirm(*a,**kw):busy[0]=True;return True
        with patch.object(m.mm,'models_root',return_value=Path('/fixture/old')),patch.object(m.QFileDialog,'getExistingDirectory',return_value='/fixture/new'),patch.object(m.dialogs,'confirm',side_effect=confirm),patch.object(m,'ModelsRelocateWorker') as worker:
            with self.assertRaises(RuntimeError):self.c.storage_change('voice')
        worker.assert_not_called()

    def test_relocation_refuses_while_install_is_owned(self):
        from unittest.mock import patch
        from tests.model_settings_fixtures import FakeWorker
        worker=FakeWorker();self.c._operation(worker,'indextts2','local_indextts2_0')
        with patch.object(m.QFileDialog,'getExistingDirectory') as picker,self.assertRaises(RuntimeError):self.c.storage_change('voice')
        picker.assert_not_called()
