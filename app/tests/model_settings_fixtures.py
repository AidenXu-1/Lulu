"""Isolated model-settings owners; never open real stores, keys or models."""
import os
import time
import unittest
from contextlib import contextmanager, ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from src.ui.model_settings import controller as m


class FakeWorker(QObject):
    finished = Signal()
    progress = Signal(str, float)
    failed = Signal(str)
    cancelled = Signal()
    settled = Signal(str)

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.args, self.kwargs = args, kwargs
        self.running = False
        self.cancel_requested = False
        self.error = None
        self.was_cancelled = False
        self.result = None

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def request_cancel(self):
        self.cancel_requested = True

    def wait(self, milliseconds):
        return not self.running

    def finish(self):
        self.running = False
        self.finished.emit()


class ControllerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = Mock()
        self.service.snapshot.return_value = dict(providers=[], asrProviders=[], connections=[], catalogue={})
        p = patch('src.core.model_connections.Service', return_value=self.service)
        p.start()
        self.addCleanup(p.stop)
        self.cfg = SimpleNamespace(engine_name='local_sensevoice', default_tts_model='local_indextts2', theme='warm', save=Mock(return_value=True))
        self.changed = Mock()
        self.c = m.ModelController(self.cfg, on_changed=self.changed)
        self.c.refresh = Mock()
        self.notices = []
        self.c.notice.connect(self.notices.append)
        self.addCleanup(self.cleanup_controller)

    def cleanup_controller(self):
        self.c.shutdown_workers(100)
        for worker in list(self.c.owned_workers()):
            if isinstance(worker, FakeWorker):
                worker.finish()
            elif worker.isRunning():
                worker.wait(3000)
        self.app.processEvents()

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(predicate())

    @contextmanager
    def public_state(self, **overrides):
        from src.core.synthesis import qwen3_deployment as dp, qwen3_weights as qw
        with ExitStack() as scope:
            defaults = {
                (m.mm,'is_downloaded'):False, (m.mm,'needs_repair'):False,
                (m.mm,'runtime_issue'):None, (m.mm,'resumable_download_size'):0,
                (m.mm,'storage_dir'):Path('/fixture/models'), (m.rp,'status'):'not_installed',
                (m.rp,'current_platform'):'mac-arm64', (m.rp,'partial_install_bytes'):0,
                (m.rp,'is_runtime_installed'):False, (m.rp,'tts_root'):Path('/fixture/tts'),
                (dp,'read_state'):{'features':{}}, (dp,'root'):Path('/fixture/qwen'),
                (qw,'is_installed'):False,
            }
            for (owner,name),value in defaults.items():
                scope.enter_context(patch.object(owner,name,return_value=overrides.get(name,value)))
            scope.enter_context(patch.object(m,'available_local_models',return_value=list(m.LOCAL_MODELS.values())))
            yield
