"""Model status replies must not access destroyed pages or child editors."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QPlainTextEdit, QWidget
import shiboken6

from src.ui.voice_page import VoicePage


class Signal:
    def connect(self, callback): self.callback = callback


class Worker:
    def __init__(self, _read):
        self.result, self.finished = Signal(), Signal()
        self.deleted = False
    def start(self): pass
    def deleteLater(self): self.deleted = True


class Page(QWidget):
    model_readiness = VoicePage.model_readiness
    def __init__(self):
        super().__init__()
        self._model_status_cache = {}
        self._model_status_reads = {}
        self._model_status_closing = False
        self._model_status_epoch = 0
        editor = QPlainTextEdit(self)
        self.calls = []
        self._qwen3 = SimpleNamespace(text_edit=editor,
            sync_text=lambda: self.calls.append(editor.toPlainText()))
        self.events = 0
    def _emit_model_context_changed(self): self.events += 1


class VoiceReadinessLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def prepare(self):
        page = Page()
        with patch('src.ui.model_settings.workers.ReadWorker', Worker):
            page.model_readiness('qwen3')
        return page, page._model_status_reads['qwen3']

    def test_result_after_page_destruction_has_no_ui_or_cache_effect(self):
        page, worker = self.prepare()
        shiboken6.delete(page)
        worker.result.callback((True, 'ready'), None)
        self.assertEqual(page.calls, [])
        self.assertEqual(page._model_status_cache, {})
        self.assertEqual(page.events, 0)

    def test_result_after_editor_destruction_does_not_access_old_editor(self):
        page, worker = self.prepare()
        shiboken6.delete(page._qwen3.text_edit)
        worker.result.callback((True, 'ready'), None)
        self.assertEqual(page.calls, [])
        self.assertEqual(page.events, 0)
        shiboken6.delete(page)

    def test_settled_after_page_destruction_releases_worker_without_refresh(self):
        page, worker = self.prepare()
        page._model_status_epoch += 1
        shiboken6.delete(page)
        worker.finished.callback()
        self.assertTrue(worker.deleted)
        self.assertEqual(page._model_status_reads, {})

    def test_live_page_still_receives_status_and_closed_page_ignores_it(self):
        page, worker = self.prepare()
        worker.result.callback((True, 'ready'), None)
        self.assertEqual(page.calls, [''])
        self.assertEqual(page.events, 1)
        self.assertEqual(page._model_status_cache['qwen3'][1], (True, 'ready'))
        page._model_status_closing = True
        worker.result.callback((False, 'late'), None)
        self.assertEqual(page.events, 1)
        shiboken6.delete(page)
